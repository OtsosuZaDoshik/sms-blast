#!/usr/bin/env python3
"""Основные тесты: номера, шаблоны, импорт, рассылка, удаление базы.

Запуск:  .venv/bin/python tests/test_core.py
"""

import base64
import glob
import io
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp(prefix="smsblast-core-")
from smsblast import config  # noqa: E402
config.DB_PATH = os.path.join(TMP, "t.db")
config.UPLOAD_DIR = os.path.join(TMP, "up")
config.EXPORT_DIR = os.path.join(TMP, "ex")

import app as webapp  # noqa: E402
from smsblast import db, importer, phones, templating  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    if not cond:
        FAILS.append(name)
    print("{} {}{}".format("OK  " if cond else "FAIL", name,
                           ("  -> " + str(detail)) if detail else ""))


# --- макет шлюза -----------------------------------------------------------

SENT, STORE, FAIL_NEXT = [], {}, {"n": 0}


class Mock(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _ok(self):
        h = self.headers.get("Authorization", "")
        return h.startswith("Basic ") and \
            base64.b64decode(h[6:]).decode() == "sms:secret"

    def _json(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._ok():
            return self._json(401, {})
        if self.path == "/health":
            return self._json(200, {"releaseId": 1, "status": "pass",
                                    "version": "1.0.0",
                                    "checks": {"battery:level": {"observedValue": 90}}})
        if self.path.startswith("/message/"):
            mid = self.path.split("/message/", 1)[1]
            return self._json(200, {"id": mid, "state": STORE.get(mid, "Pending")}) \
                if mid in STORE else self._json(404, {})
        return self._json(404, {})

    def do_POST(self):
        if not self._ok():
            return self._json(401, {})
        if FAIL_NEXT["n"] > 0:
            FAIL_NEXT["n"] -= 1
            return self._json(500, {})
        n = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(n) or b"{}")
        mid = payload.get("id") or "gw-{}".format(len(STORE) + 1)
        STORE[mid] = "Pending"
        SENT.append(payload)
        return self._json(202, {"id": mid, "state": "Pending"})


server = HTTPServer(("127.0.0.1", 0), Mock)
PORT = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()

print("=== Нормализация номеров ===")
for raw, want in [("8 (900) 123-45-67", "+79001234567"),
                  ("+7 900 123 45 67", "+79001234567"),
                  ("79001234567", "+79001234567"),
                  ("9001234567", "+79001234567"),
                  ("79001234567.0", "+79001234567"),
                  ("+380 67 123 4567", "+380671234567")]:
    got, err = phones.normalize(raw, "7")
    check("номер «{}»".format(raw), got == want, "{} {}".format(got, err or ""))
for bad in ["", "абв", "123", "1" * 20]:
    got, err = phones.normalize(bad, "7")
    check("отбраковка «{}»".format(bad), got is None and err, err)

print("\n=== Сегменты SMS ===")
check("латиница = GSM-7", phones.segments("Hello")[0] == "GSM-7")
check("кириллица = UCS-2", phones.segments("Привет")[0] == "UCS-2")
check("71 símv кириллицы = 2 сегмента", phones.segments("я" * 71)[2] == 2)
check("161 символ латиницы = 2 сегмента", phones.segments("a" * 161)[2] == 2)

print("\n=== Шаблоны ===")
check("подстановка без учёта регистра",
      templating.render("Здравствуйте, {имя}! Город {Город}.",
                        {"имя": "Иван", "город": "Москва"})
      == "Здравствуйте, Иван! Город Москва.")
check("пустая переменная не оставляет мусора",
      templating.render("Привет, {имя}!", {}) == "Привет!")
check("список недостающих полей",
      templating.missing_for("{имя} {скидка}", {"имя": "И"}) == ["скидка"])

print("\n=== Импорт ===")
webapp.app.config["TESTING"] = True
client = webapp.app.test_client()
db.save_settings({"gateway_host": "127.0.0.1", "gateway_port": str(PORT),
                  "gateway_user": "sms", "gateway_password": "secret",
                  "connection_mode": "wifi"})

csv_text = ("телефон;имя;город;согласие\n"
            "8 (900) 111-22-33;Иван;Москва;да\n"
            "+7 900 222 33 44;Мария;Казань;да\n"
            "9003334455;Пётр;Уфа;нет\n"
            "8 900 111 22 33;Иван дубль;Москва;да\n"
            "мусор;Никто;;да\n")
response = client.post("/contacts/import",
                       data={"file": (io.BytesIO(csv_text.encode()), "base.csv")},
                       content_type="multipart/form-data")
html = response.get_data(as_text=True)
check("страница разметки открылась", response.status_code == 200)
check("колонка телефона угадана", 'value="телефон" selected' in html)
path = html.split('name="path" value="', 1)[1].split('"', 1)[0]
client.post("/contacts/import/confirm",
            data={"path": path, "phone_column": "телефон", "name_column": "имя",
                  "consent_column": "согласие", "default_consent": "on"},
            follow_redirects=True)
check("импортировано 3 контакта", db.count_contacts() == 3, db.count_contacts())
check("с согласием 2", db.count_contacts(only_consent=True) == 2)

print("\n=== Разделитель CSV при запятых внутри значений ===")
tricky = ('телефон;имя;поля\n'
          '+79005556677;Тест;"{""a"": ""1"", ""b"": ""2"", ""c"": ""3""}"\n')
p = os.path.join(TMP, "tricky.csv")
open(p, "w", encoding="utf-8").write(tricky)
headers, rows = importer.read_table(p)
check("разделитель определён верно", headers == ["телефон", "имя", "поля"], headers)

print("\n=== Стоп-лист ===")
client.post("/optout", data={"phones": "8 900 222 33 44", "reason": "тест"},
            follow_redirects=True)
check("номер в стоп-листе", db.is_optout("+79002223344"))

print("\n=== Кампания ===")
preview = client.post("/campaigns/preview",
                      json={"template": "Здравствуйте, {имя}! Скидка в {город}.",
                            "only_consent": True}).get_json()
check("предпросмотр исключает стоп-лист",
      all(s["phone"] != "+79002223344" for s in preview["samples"]), preview)
check("счётчик аудитории совпадёт с очередью", preview["total"] == 1, preview["total"])

response = client.post("/campaigns/new", data={
    "name": "Тест", "template": "Здравствуйте, {имя}! Скидка в {город}. СТОП",
    "only_consent": "on", "delay_sec": "0", "jitter_sec": "0",
    "hourly_limit": "1000", "sim_number": "0"}, follow_redirects=False)
cid = int(response.headers["Location"].rstrip("/").split("/")[-1])
check("в очереди 1 получатель", db.campaign_stats(cid).get("queued") == 1,
      db.campaign_stats(cid))

FAIL_NEXT["n"] = 2  # шлюз падает дважды — проверяем повторы
client.post("/campaigns/{}/start".format(cid), follow_redirects=True)
deadline = time.time() + 40
while time.time() < deadline and db.get_campaign(cid)["status"] != "done":
    time.sleep(0.4)
check("кампания завершена", db.get_campaign(cid)["status"] == "done",
      db.get_campaign(cid)["status"])
check("отправлено ровно одно SMS", len(SENT) == 1, len(SENT))
check("повторы после ошибок 500 сработали", FAIL_NEXT["n"] == 0)
if SENT:
    check("получатель верный", SENT[0]["phoneNumbers"] == ["+79001112233"])
    check("текст персонализирован", "Иван" in SENT[0]["textMessage"]["text"]
          and "Москва" in SENT[0]["textMessage"]["text"], SENT[0])
    check("свой id для идемпотентности", SENT[0]["id"].startswith("smsblast-"))

print("\n=== Отчёт ===")
response = client.get("/campaigns/{}/export.csv".format(cid))
body = response.get_data(as_text=True)
check("CSV выгружается", response.status_code == 200 and "+79001112233" in body)
check("BOM для Excel", body.startswith("﻿"))

print("\n=== Удаление всех контактов ===")
for bad in ["", "удали", "DELETE", "да"]:
    client.post("/contacts/delete-all", data={"confirm": bad}, follow_redirects=True)
check("неверные подтверждения ничего не удалили", db.count_contacts() == 3,
      db.count_contacts())

html = client.get("/contacts?q=Иван").get_data(as_text=True)
check("при поиске показано число всей базы", "все 3" in html)

response = client.post("/contacts/delete-all", data={"confirm": "УДАЛИТЬ"},
                       follow_redirects=True)
check("контакты удалены", db.count_contacts() == 0, db.count_contacts())
check("сообщено количество", "Удалено контактов: 3" in response.get_data(as_text=True))
check("стоп-лист уцелел", db.is_optout("+79002223344"))
check("история кампании цела", db.campaign_stats(cid).get("sent")
      or db.campaign_stats(cid).get("delivered"), db.campaign_stats(cid))

backups = glob.glob(os.path.join(config.EXPORT_DIR, "contacts-backup-*.csv"))
check("резервная копия создана", len(backups) == 1, backups)
if backups:
    headers, rows = importer.read_table(backups[0])
    check("копия читается обратно", len(rows) == 3 and "телефон" in headers,
          (len(rows), headers))
    result = importer.import_rows(rows, phone_column="телефон", name_column="имя",
                                  consent_column="согласие", country_code="7",
                                  source="восстановление")
    # Один из трёх номеров в стоп-листе — он не должен вернуться в базу
    # через восстановление, иначе отписка обходится перезаливкой копии.
    check("база восстановлена из копии", db.count_contacts() == 2,
          (db.count_contacts(), result))
    check("отписавшийся не вернулся", result["optout"] == 1
          and not any(r["phone"] == "+79002223344" for r in db.list_contacts()),
          result)
    check("остальные восстановлены с именами",
          sorted(r["name"] for r in db.list_contacts()) == ["Иван", "Пётр"],
          [r["name"] for r in db.list_contacts()])

print("\n=== Вывод на системах без кириллицы (Windows cp1252) ===")
import desktop  # noqa: E402

def _cp1252_stream():
    return io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")

real_out, real_err = sys.stdout, sys.stderr
sys.stdout = sys.stderr = _cp1252_stream()
try:
    print("SMS-рассылка")
    crashes = False
except UnicodeEncodeError:
    crashes = True
sys.stdout, sys.stderr = real_out, real_err
check("поток cp1252 действительно не принимает кириллицу", crashes)

sys.stdout = sys.stderr = _cp1252_stream()
try:
    desktop.ensure_output()
    print("SMS-рассылка: http://127.0.0.1:5001")
    printed = True
except UnicodeEncodeError:
    printed = False
finally:
    sys.stdout, sys.stderr = real_out, real_err
check("после ensure_output кириллица печатается без падения", printed)

sys.stdout = sys.stderr = None
try:
    handle = desktop.ensure_output(os.path.join(TMP, "app.log"))
    print("SMS-рассылка")
    no_stdout_ok = True
except Exception:
    no_stdout_ok = False
finally:
    sys.stdout, sys.stderr = real_out, real_err
check("при отсутствующем stdout вывод уходит в журнал", no_stdout_ok)
check("журнал создан и содержит кириллицу",
      os.path.exists(os.path.join(TMP, "app.log"))
      and "рассылка" in open(os.path.join(TMP, "app.log"), encoding="utf-8").read())

print("\n=== Страницы интерфейса ===")
for url in ["/", "/contacts", "/optout", "/settings", "/campaigns/new",
            "/contacts/import", "/campaigns/{}".format(cid),
            "/campaigns/{}/progress.json".format(cid)]:
    check("страница {}".format(url), client.get(url).status_code == 200)

html = client.get("/").get_data(as_text=True)
check("боковая навигация на месте", "side-nav" in html)
check("активный пункт подсвечен", 'class="on"' in html)

print("\n" + "=" * 55)
print("ПРОВАЛЕНО {}: {}".format(len(FAILS), "; ".join(FAILS)) if FAILS
      else "Все проверки пройдены.")
sys.exit(1 if FAILS else 0)
