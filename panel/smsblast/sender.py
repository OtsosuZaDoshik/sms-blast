"""Фоновый воркер рассылки: очередь, темп, повторы, статусы доставки.

Одновременно идёт только одна кампания — SIM-карта в телефоне одна,
и параллельные отправки лишь ускорили бы блокировку номера оператором.
"""

import logging
import random
import threading
import time

from . import db, gateway as gw, usb

log = logging.getLogger("smsblast.sender")

# После стольких подряд ошибок связи кампания уходит на паузу,
# чтобы не сжечь все попытки, пока телефон недоступен.
CONSECUTIVE_FAILURES_LIMIT = 5
STATUS_POLL_INTERVAL = 60


class Sender(object):
    def __init__(self):
        self._thread = None
        self._poller = None
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self.state = {"message": "", "error": "", "current": ""}

    # --- управление ---------------------------------------------------------

    def ensure_running(self):
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, name="sender",
                                                daemon=True)
                self._thread.start()
            if self._poller is None or not self._poller.is_alive():
                self._poller = threading.Thread(target=self._poll_statuses,
                                                name="status-poller", daemon=True)
                self._poller.start()

    def start_campaign(self, campaign_id):
        db.set_campaign_status(campaign_id, "running")
        self.state["error"] = ""
        self.state["message"] = "Рассылка запущена"
        self.ensure_running()
        self._wake.set()

    def pause_campaign(self, campaign_id, reason=""):
        db.set_campaign_status(campaign_id, "paused")
        self.state["message"] = reason or "Пауза"
        self._wake.set()

    def stop_campaign(self, campaign_id):
        db.set_campaign_status(campaign_id, "stopped")
        self.state["message"] = "Рассылка остановлена"
        self._wake.set()

    # --- основной цикл ------------------------------------------------------

    def _loop(self):
        failures = 0
        while True:
            campaign_id = db.running_campaign_id()
            if campaign_id is None:
                self.state["current"] = ""
                self._wake.wait(2)
                self._wake.clear()
                continue

            campaign = db.get_campaign(campaign_id)
            message = db.next_queued(campaign_id)
            if message is None:
                db.set_campaign_status(campaign_id, "done")
                self.state["message"] = "Кампания «{}» завершена".format(campaign["name"])
                self.state["current"] = ""
                continue

            # Лимит в час: ждём, пока освободится окно.
            limit = int(campaign["hourly_limit"] or 0)
            if limit and db.sent_last_hour(campaign_id) >= limit:
                self.state["message"] = (
                    "Достигнут лимит {} SMS/час — ждём освобождения окна".format(limit)
                )
                if self._sleep(60, campaign_id):
                    continue
                continue

            if db.is_optout(message["phone"]):
                db.mark_message(message["id"], "skipped", error="номер в стоп-листе")
                continue

            settings = db.get_settings()
            client = gw.from_settings(settings)
            max_attempts = int(settings.get("max_attempts", 3))
            self.state["current"] = message["phone"]

            try:
                gateway_id, _state = client.send(
                    phone=message["phone"],
                    text=message["text"],
                    sim_number=int(campaign["sim_number"] or 0),
                    message_id="smsblast-{}-{}".format(campaign_id, message["id"]),
                )
                db.mark_message(message["id"], "sent", gateway_id=gateway_id,
                                bump_attempt=True)
                failures = 0
                self.state["error"] = ""
                self.state["message"] = "Отправлено на {}".format(message["phone"])

            except gw.GatewayError as exc:
                attempts = message["attempts"] + 1
                self.state["error"] = exc.full()

                if not exc.transient:
                    db.mark_message(message["id"], "failed", error=exc.full(),
                                    bump_attempt=True)
                    self.pause_campaign(
                        campaign_id, "Пауза: {}".format(exc.full())
                    )
                    continue

                if attempts >= max_attempts:
                    db.mark_message(message["id"], "failed", error=exc.full(),
                                    bump_attempt=True)
                else:
                    # Оставляем в очереди — повторим после паузы.
                    db.mark_message(message["id"], "queued", error=exc.full(),
                                    bump_attempt=True)

                # По кабелю проброс порта переживает не всё: перевтыкание,
                # перезагрузку телефона, засыпание adb. Поднимаем его сами.
                if settings.get("connection_mode") == "usb":
                    try:
                        usb.ensure_forward(
                            int(settings.get("usb_local_port", 18080) or 18080),
                            int(settings.get("gateway_port", 8080) or 8080),
                        )
                        self.state["message"] = "Проброс по USB восстановлен"
                    except usb.UsbError as usb_exc:
                        self.state["error"] = usb_exc.full()

                failures += 1
                if failures >= CONSECUTIVE_FAILURES_LIMIT:
                    self.pause_campaign(
                        campaign_id,
                        "Пауза: {} ошибок связи подряд. {}".format(failures, exc.full()),
                    )
                    failures = 0
                    continue

                self._sleep(min(5 * attempts, 30), campaign_id)
                continue

            delay = float(campaign["delay_sec"] or 0)
            jitter = float(campaign["jitter_sec"] or 0)
            self._sleep(delay + random.uniform(0, jitter), campaign_id)

    def _sleep(self, seconds, campaign_id):
        """Прерываемая пауза. True, если кампанию сняли с выполнения."""
        deadline = time.time() + seconds
        while time.time() < deadline:
            if db.running_campaign_id() != campaign_id:
                return True
            time.sleep(min(0.5, max(0.05, deadline - time.time())))
        return False

    # --- опрос статусов доставки -------------------------------------------

    def _poll_statuses(self):
        while True:
            time.sleep(STATUS_POLL_INTERVAL)
            try:
                pending = db.pending_status_checks()
                if not pending:
                    continue
                client = gw.from_settings(db.get_settings())
                for message in pending:
                    try:
                        status = client.status(message["gateway_id"])
                    except gw.GatewayError:
                        break  # шлюз недоступен — попробуем в следующий раз
                    if status and status != "sent":
                        db.mark_message(message["id"], status)
            except Exception:  # поток не должен умирать из-за одной ошибки
                log.exception("сбой опроса статусов")


sender = Sender()
