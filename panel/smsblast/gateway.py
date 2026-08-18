"""HTTP-клиент к приложению SMS Gateway for Android (режим Local Server).

Контракт API:
    POST /message   {"textMessage": {"text": "..."}, "phoneNumbers": ["+7..."]}
                    -> {"id": "...", "state": "Pending"}
    GET  /message/{id}  -> статус конкретного сообщения
    GET  /health        -> проверка, что шлюз жив
Авторизация — HTTP Basic, логин и пароль показаны в приложении на телефоне.
"""

import errno
import socket

import requests
from requests.auth import HTTPBasicAuth

# Статусы шлюза → наши внутренние.
STATE_MAP = {
    "Pending": "sent",
    "Processed": "sent",
    "Sent": "sent",
    "Delivered": "delivered",
    "Failed": "failed",
    "Cancelled": "failed",
    "Cancelling": "sent",
}


class GatewayError(Exception):
    """Ошибка обращения к шлюзу. transient=True — имеет смысл повторить.

    hint — что именно чинить: «таймаут» и «порт закрыт» ломаются в разных
    местах, и общая формулировка «нет связи» заставляет искать вслепую.
    """

    def __init__(self, message, transient=True, hint=""):
        super().__init__(message)
        self.transient = transient
        self.hint = hint

    def full(self):
        return "{} — {}".format(self, self.hint) if self.hint else str(self)


class Gateway(object):
    def __init__(self, host, port=8080, user="sms", password="", timeout=20,
                 mode="wifi"):
        self.host = str(host).strip()
        self.port = int(port)
        self.mode = mode
        self.base = "http://{}:{}".format(self.host, self.port)
        self.auth = HTTPBasicAuth(user, password)
        self.timeout = timeout

    # --- служебное ----------------------------------------------------------

    def _request(self, method, path, **kwargs):
        url = self.base + path
        try:
            return requests.request(
                method, url, auth=self.auth, timeout=self.timeout, **kwargs
            )
        except requests.exceptions.RequestException as exc:
            raise self._diagnose(exc)
        except UnicodeEncodeError:
            # Basic-авторизация кодируется в latin-1: кириллица в логине или
            # пароле роняет запрос ещё до отправки. Обычно это промах раскладки.
            raise GatewayError(
                "логин или пароль содержит нелатинские символы",
                transient=False,
                hint="в этих полях допустимы только латиница, цифры и знаки — "
                     "проверьте раскладку клавиатуры",
            )

    def probe_port(self, timeout=3.0):
        """Что происходит на уровне TCP: open | refused | unreachable |
        timeout | dns | unknown.

        Тип исключения requests для «хост молчит» плавает между ConnectTimeout,
        ReadTimeout и ConnectionError в зависимости от цели и формы таймаута,
        поэтому причину выясняем сами — одним честным connect().
        """
        try:
            infos = socket.getaddrinfo(self.host, self.port, socket.AF_INET,
                                       socket.SOCK_STREAM)
        except socket.gaierror:
            return "dns"
        if not infos:
            return "dns"

        family, socktype, proto, _canon, sockaddr = infos[0]
        sock = socket.socket(family, socktype, proto)
        sock.settimeout(timeout)
        try:
            code = sock.connect_ex(sockaddr)
        except socket.timeout:
            return "timeout"
        except OSError:
            return "unreachable"
        finally:
            sock.close()

        if code == 0:
            return "open"
        if code == errno.ECONNREFUSED:
            return "refused"
        if code in (errno.EHOSTUNREACH, errno.ENETUNREACH,
                    errno.EHOSTDOWN, errno.ENETDOWN):
            return "unreachable"
        if code in (errno.ETIMEDOUT, errno.EWOULDBLOCK,
                    errno.EAGAIN, errno.EINPROGRESS):
            return "timeout"
        return "unknown"

    def _diagnose(self, exc):
        """Превращает сетевой сбой в понятную причину с подсказкой."""
        state = self.probe_port()

        if state == "open":
            return GatewayError(
                "шлюз принял соединение, но не ответил вовремя",
                hint="телефон подвис или усыплён: разбудите экран и отключите "
                     "оптимизацию батареи для приложения",
            )
        if state == "refused":
            if self.mode == "usb":
                return GatewayError(
                    "порт {} на Mac никто не слушает".format(self.port),
                    hint="проброс adb отвалился (кабель вынули или телефон "
                         "перезагрузился) — нажмите «Подключить по USB»; если "
                         "проброс на месте, включите Local Server в приложении",
                )
            return GatewayError(
                "порт {} закрыт — телефон в сети, но шлюз не слушает".format(self.port),
                hint="в приложении включите переключатель Local Server "
                     "и нажмите кнопку Offline",
            )
        if state == "unreachable":
            return GatewayError(
                "устройства {} нет в сети".format(self.host),
                hint="у телефона сменился IP, он ушёл на мобильный интернет "
                     "или в другую сеть — нажмите «Найти телефон»",
            )
        if state == "timeout":
            return GatewayError(
                "телефон не отвечает по адресу {}".format(self.host),
                hint="устройства нет в сети или у него сменился IP — "
                     "нажмите «Найти телефон»",
            )
        if state == "dns":
            return GatewayError(
                "адрес «{}» не разрешается".format(self.host),
                transient=False,
                hint="в поле адреса должен быть IP вида 192.168.1.50",
            )
        return GatewayError(
            "нет связи с телефоном ({})".format(type(exc).__name__),
            hint="проверьте Wi-Fi на обоих устройствах и адрес шлюза",
        )

    @staticmethod
    def _check(response):
        if response.status_code in (401, 403):
            raise GatewayError(
                "шлюз отверг логин/пароль", transient=False,
                hint="пароль меняется при перезапуске Local Server — "
                     "перепишите его из приложения заново",
            )
        if response.status_code >= 500:
            raise GatewayError("шлюз вернул {}".format(response.status_code))
        if response.status_code >= 400:
            raise GatewayError(
                "шлюз вернул {}: {}".format(response.status_code, response.text[:200]),
                transient=False,
            )
        return response

    # --- API ----------------------------------------------------------------

    def health(self):
        """(ok, описание) — для кнопки «Проверить связь»."""
        try:
            response = self._request("GET", "/health")
        except GatewayError as exc:
            return False, exc.full()
        if response.status_code in (401, 403):
            return False, ("связь есть, но логин/пароль неверные — пароль меняется "
                           "при перезапуске Local Server, перепишите его заново")
        if response.status_code < 400:
            return True, "шлюз на связи ({})".format(self.base)
        return False, "шлюз ответил {}".format(response.status_code)

    def send(self, phone, text, sim_number=0, message_id=None, with_delivery_report=True):
        """Ставит SMS в очередь телефона. Возвращает (gateway_id, state)."""
        payload = {
            "textMessage": {"text": text},
            "phoneNumbers": [phone],
            "withDeliveryReport": bool(with_delivery_report),
        }
        if sim_number:
            payload["simNumber"] = int(sim_number)
        if message_id:
            # Свой id делает повтор идемпотентным: шлюз не отправит дубль.
            payload["id"] = message_id

        response = self._request("POST", "/message", json=payload)

        # Старые сборки не принимают кастомный id — повторяем без него.
        if response.status_code == 400 and message_id:
            payload.pop("id")
            response = self._request("POST", "/message", json=payload)

        self._check(response)
        try:
            data = response.json()
        except ValueError:
            raise GatewayError("шлюз вернул не JSON: {}".format(response.text[:200]))
        return data.get("id"), data.get("state", "Pending")

    def status(self, gateway_id):
        """Внутренний статус или None, если шлюз не смог ничего сказать."""
        response = self._request("GET", "/message/{}".format(gateway_id))
        if response.status_code == 404:
            return None
        if response.status_code in (400, 405):
            # На части версий одиночного GET нет — берём из списка.
            return self._status_from_list(gateway_id)
        self._check(response)
        try:
            data = response.json()
        except ValueError:
            return None
        return STATE_MAP.get(data.get("state"))

    def _status_from_list(self, gateway_id):
        response = self._request("GET", "/messages", params={"ids": gateway_id})
        if response.status_code >= 400:
            return None
        try:
            items = response.json()
        except ValueError:
            return None
        if isinstance(items, dict):
            items = items.get("data") or items.get("messages") or []
        for item in items:
            if item.get("id") == gateway_id:
                return STATE_MAP.get(item.get("state"))
        return None


def from_settings(settings):
    """Клиент по текущему способу подключения.

    В режиме USB шлюз доступен на 127.0.0.1: порт проброшен adb, поэтому
    адрес телефона и состояние Wi-Fi не имеют значения.
    """
    if settings.get("connection_mode") == "usb":
        host = "127.0.0.1"
        port = settings.get("usb_local_port", 18080)
    else:
        host = settings.get("gateway_host", "")
        port = settings.get("gateway_port", 8080)

    return Gateway(
        host=host,
        port=port,
        user=settings.get("gateway_user", "sms"),
        password=settings.get("gateway_password", ""),
        mode=settings.get("connection_mode", "wifi"),
    )
