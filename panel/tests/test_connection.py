#!/usr/bin/env python3
"""Тесты связи с телефоном: поиск шлюза, сетевые ошибки, USB через adb.

Запуск:  .venv/bin/python tests/test_connection.py
Двойник adb создаётся на лету — настоящий телефон не нужен.
"""

import base64
import ipaddress
import json
import os
import socket
import stat
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp(prefix="smsblast-conn-")
FAKE = os.path.join(TMP, "bin")
os.makedirs(FAKE)
STATE = os.path.join(TMP, "mode")
FORWARDS = os.path.join(TMP, "forwards")

open(os.path.join(FAKE, "adb"), "w").write(r'''#!/bin/bash
mode=$(cat "$ADB_FAKE_STATE" 2>/dev/null || echo ready)
args=(); skip=0
for a in "$@"; do
  if [ $skip -eq 1 ]; then skip=0; continue; fi
  if [ "$a" = "-s" ]; then skip=1; continue; fi
  args+=("$a")
done
# Журнал вызовов — тесты проверяют, что именно было отправлено телефону.
echo "$*" >> "$ADB_FAKE_CALLS"

case "${args[0]}" in
  shell)
    case "${args[1]} ${args[2]}" in
      "pm list")
        [ "$mode" = "nogateway" ] || echo "package:me.capcom.smsgateway"
        exit 0 ;;
      "pm grant")
        [ "$mode" = "nogrant" ] && { echo "Operation not allowed" >&2; exit 1; }
        exit 0 ;;
      "dumpsys package")
        echo "    versionName=1.70.4"
        echo "    android.permission.SEND_SMS: granted=true"
        echo "    android.permission.READ_PHONE_STATE: granted=true"
        exit 0 ;;
      "dumpsys deviceidle")
        [ "${args[3]}" = "whitelist" ] && echo "user,me.capcom.smsgateway,10597"
        exit 0 ;;
      "am start"|"am broadcast")
        echo "Broadcast completed: result=0"; exit 0 ;;
      *) exit 0 ;;
    esac ;;
  devices)
    echo "List of devices attached"
    case "$mode" in
      none) ;;
      unauthorized) echo "R58N90ABCDE  unauthorized usb:1234X" ;;
      offline)      echo "R58N90ABCDE  offline transport_id:1" ;;
      multiple)
        echo "R58N90ABCDE  device usb:1D model:SM_A515F transport_id:1"
        echo "ZY22LKJH99   device usb:2D model:Redmi_Note_11 transport_id:2" ;;
      *) echo "R58N90ABCDE  device usb:1D model:SM_A515F transport_id:1" ;;
    esac ;;
  forward)
    if [ "${args[1]}" = "--list" ]; then
      [ -f "$ADB_FAKE_FORWARDS" ] && cat "$ADB_FAKE_FORWARDS"; exit 0; fi
    if [ "${args[1]}" = "--remove" ]; then : > "$ADB_FAKE_FORWARDS"; exit 0; fi
    if [ "$mode" = "portbusy" ]; then
      echo "adb: error: cannot bind listener: Address already in use" >&2; exit 1; fi
    echo "R58N90ABCDE ${args[1]} ${args[2]}" > "$ADB_FAKE_FORWARDS"; exit 0 ;;
  *) exit 1 ;;
esac
''')
os.chmod(os.path.join(FAKE, "adb"), stat.S_IRWXU)
os.environ["ADB_FAKE_STATE"] = STATE
os.environ["ADB_FAKE_FORWARDS"] = FORWARDS
CALLS = os.path.join(TMP, "calls")
os.environ["ADB_FAKE_CALLS"] = CALLS
os.environ["PATH"] = FAKE + os.pathsep + os.environ["PATH"]

from smsblast import config  # noqa: E402
config.DB_PATH = os.path.join(TMP, "t.db")
config.UPLOAD_DIR = os.path.join(TMP, "up")
config.EXPORT_DIR = os.path.join(TMP, "ex")

import app as webapp  # noqa: E402
from smsblast import db, discovery, gateway as gw, usb  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    if not cond:
        FAILS.append(name)
    print("{} {}{}".format("OK  " if cond else "FAIL", name,
                           ("  -> " + str(detail)) if detail else ""))


def set_mode(mode):
    open(STATE, "w").write(mode)
    if os.path.exists(FORWARDS):
        os.remove(FORWARDS)


HEALTH = {"releaseId": 1, "status": "pass", "version": "1.0.0",
          "checks": {"battery:level": {"observedValue": 94, "status": "pass"},
                     "connection:status": {"observedValue": 1, "status": "pass"},
                     "messages:failed": {"observedValue": 0, "status": "pass"}}}
SENT = []


def make_server(mode):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _authed(self):
            h = self.headers.get("Authorization", "")
            return h.startswith("Basic ") and \
                base64.b64decode(h[6:]).decode() == "sms:secret"

        def _send(self, code, body, ctype="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if mode == "auth" and not self._authed():
                return self._send(401, b"")
            if mode == "html":
                return self._send(200, b"<html>Printer</html>", "text/html")
            return self._send(200, json.dumps(HEALTH).encode())

        def do_POST(self):
            if not self._authed():
                return self._send(401, b"")
            n = int(self.headers.get("Content-Length", 0))
            SENT.append(json.loads(self.rfile.read(n) or b"{}"))
            return self._send(202, json.dumps({"id": "x", "state": "Pending"}).encode())

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv.server_address[1]


print("=== Опознание шлюза по /health ===")
check("настоящий health опознан", discovery.looks_like_gateway(HEALTH))
check("подпись читаема", "94%" in discovery.describe(HEALTH), discovery.describe(HEALTH))
for junk in [{}, {"status": "ok"}, {"status": "pass"}, [],
             {"status": "pass", "checks": {"disk": {}}}]:
    check("чужой ответ не принят: {}".format(junk),
          not discovery.looks_like_gateway(junk))

print("\n=== Классификация устройств ===")
port_gw, port_auth, port_html = make_server("gateway"), make_server("auth"), make_server("html")
check("шлюз -> gateway", discovery._probe("127.0.0.1", port_gw, 3)["kind"] == "gateway")
check("требует пароль -> maybe", discovery._probe("127.0.0.1", port_auth, 3)["kind"] == "maybe")
check("веб-панель принтера -> other", discovery._probe("127.0.0.1", port_html, 3)["kind"] == "other")

print("\n=== Подсети Mac ===")
nets = discovery.local_networks()
check("сеть найдена", len(nets) >= 1, [(i, str(n)) for i, _, n in nets])
check("VPN исключены", all(not i.startswith("utun") for i, _, _ in nets))
check("сеть не больше 1024 адресов", all(n.num_addresses <= 1024 for _, _, n in nets))

print("\n=== Сканирование и неразглашение пароля ===")
real_networks = discovery.local_networks
discovery.local_networks = lambda *a, **k: [
    ("test0", "127.0.0.9", ipaddress.ip_network("127.0.0.0/30"))]
cands, scanned = discovery.scan(port=port_gw, connect_timeout=0.4)
check("шлюз найден сканированием",
      any(c["kind"] == "gateway" and c["host"] == "127.0.0.1" for c in cands), cands)

SEEN = []


class Spy(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        SEEN.append(self.headers.get("Authorization"))
        self.send_response(200); self.send_header("Content-Length", "2")
        self.end_headers(); self.wfile.write(b"{}")


spy = HTTPServer(("127.0.0.1", 0), Spy)
threading.Thread(target=spy.serve_forever, daemon=True).start()
db.save_settings({"gateway_user": "sms", "gateway_password": "secret"})
discovery.scan(port=spy.server_address[1], connect_timeout=0.4)
check("пароль при сканировании не отправляется",
      SEEN and all(h is None for h in SEEN), SEEN)
discovery.local_networks = real_networks

print("\n=== Точность сетевых ошибок ===")
closed = socket.socket(); closed.bind(("127.0.0.1", 0))
dead = closed.getsockname()[1]; closed.close()
try:
    gw.Gateway("127.0.0.1", dead, "sms", "secret", timeout=3).send("+79001112233", "t")
    check("закрытый порт даёт ошибку", False)
except gw.GatewayError as e:
    check("закрытый порт -> «порт закрыт»", "закрыт" in str(e) and "таймаут" not in str(e), e.full())
    check("подсказка про Local Server", "Local Server" in e.hint)

silent = socket.socket(); silent.bind(("127.0.0.1", 0)); silent.listen(5)
try:
    gw.Gateway("127.0.0.1", silent.getsockname()[1], "sms", "s", timeout=2).send("+7900", "t")
    check("молчащий шлюз даёт ошибку", False)
except gw.GatewayError as e:
    check("соединение есть, ответа нет -> «не ответил вовремя»", "вовремя" in str(e), e.full())
silent.close()

ABSENT = None
for _i, own, net in real_networks():
    router = str(net.network_address + 1)
    for host in reversed(list(net.hosts())):
        ip = str(host)
        if ip in (own, router):
            continue
        if all(gw.Gateway(ip, 8080).probe_port(timeout=2) in ("timeout", "unreachable")
               for _ in range(2)):
            ABSENT = ip
            break
    if ABSENT:
        break
if ABSENT:
    try:
        gw.Gateway(ABSENT, 8080, "sms", "s", timeout=4).send("+79001112233", "t")
        check("отсутствующий адрес даёт ошибку", False)
    except gw.GatewayError as e:
        check("телефона нет в сети -> не «не ответил вовремя»",
              ("не отвечает" in str(e) or "нет в сети" in str(e)) and "вовремя" not in str(e),
              e.full())
        check("подсказка ведёт к поиску телефона", "Найти телефон" in e.hint)

check("открытый порт -> open", gw.Gateway("127.0.0.1", port_gw).probe_port() == "open")
check("закрытый порт -> refused", gw.Gateway("127.0.0.1", dead).probe_port() == "refused")
check("плохое имя -> dns", gw.Gateway("нет-такого.invalid", 8080).probe_port() == "dns")

ok, note = gw.Gateway("127.0.0.1", port_gw, "sms", "СЕКРЕТ", timeout=3).health()
check("кириллица в пароле не роняет панель", not ok and "нелатинские" in note, note)

print("\n=== Определение подсетей на Windows ===")
PS_OUT = json.dumps([
    {"IPAddress": "192.168.1.42", "PrefixLength": 24, "InterfaceAlias": "Wi-Fi"},
    {"IPAddress": "127.0.0.1", "PrefixLength": 8, "InterfaceAlias": "Loopback"},
    {"IPAddress": "169.254.10.5", "PrefixLength": 16, "InterfaceAlias": "Ethernet 2"},
    {"IPAddress": "172.28.0.1", "PrefixLength": 20, "InterfaceAlias": "vEthernet (WSL)"},
    {"IPAddress": "10.0.75.1", "PrefixLength": 8, "InterfaceAlias": "Ethernet"},
])
nets = discovery.parse_powershell_networks(PS_OUT)
names = [n[0] for n in nets]
check("Wi-Fi найден", "Wi-Fi" in names, names)
check("loopback отброшен", "Loopback" not in names, names)
check("самоназначенный 169.254 отброшен", "Ethernet 2" not in names, names)
check("виртуальный адаптер WSL отброшен",
      not any("WSL" in n for n in names), names)
check("широкая сеть /8 сужена до /24",
      any(str(n[2]) == "10.0.75.0/24" for n in nets), [str(n[2]) for n in nets])
check("сеть Wi-Fi разобрана верно",
      any(str(n[2]) == "192.168.1.0/24" for n in nets), [str(n[2]) for n in nets])

single = json.dumps({"IPAddress": "192.168.5.7", "PrefixLength": 24,
                     "InterfaceAlias": "Wi-Fi"})
check("одиночный адаптер (JSON-объект, а не массив)",
      len(discovery.parse_powershell_networks(single)) == 1,
      discovery.parse_powershell_networks(single))

IFCONFIG = """en0: flags=8863<UP,BROADCAST> mtu 1500
\tinet 192.168.4.181 netmask 0xffffff00 broadcast 192.168.4.255
utun6: flags=8051<UP,POINTOPOINT> mtu 1400
\tinet 172.16.0.1 netmask 0xffffffff
lo0: flags=8049<UP,LOOPBACK> mtu 16384
\tinet 127.0.0.1 netmask 0xff000000
"""
nets = discovery.parse_ifconfig_networks(IFCONFIG)
check("ifconfig: найден en0", [n[0] for n in nets] == ["en0"], [n[0] for n in nets])
check("ifconfig: VPN и loopback отброшены", len(nets) == 1, nets)
check("ifconfig: маска переведена в префикс",
      str(nets[0][2]) == "192.168.4.0/24", str(nets[0][2]))

print("\n=== Пути к adb по операционным системам ===")
check("для Windows пути ведут к adb.exe",
      all(c.lower().endswith("adb.exe") for c in usb.WINDOWS_CANDIDATES),
      usb.WINDOWS_CANDIDATES)
check("для Windows учтены Sdk, scoop и chocolatey",
      any("Sdk" in c for c in usb.WINDOWS_CANDIDATES)
      and any("scoop" in c for c in usb.WINDOWS_CANDIDATES)
      and any("chocolatey" in c for c in usb.WINDOWS_CANDIDATES),
      usb.WINDOWS_CANDIDATES)
check("для macOS путь через homebrew",
      any("homebrew" in c for c in usb.UNIX_CANDIDATES), usb.UNIX_CANDIDATES)
check("подсказка на macOS про brew", "brew" in usb.install_hint())
USB_SOURCE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "smsblast", "usb.py")
check("сообщения не привязаны к Mac",
      "к Mac" not in open(USB_SOURCE, encoding="utf-8").read())

print("\n=== USB через adb ===")
set_mode("ready")
found = usb.devices()
check("устройство распознано", len(found) == 1 and found[0]["state"] == "device", found)
check("модель читаема", found[0]["model"] == "SM A515F", found[0])

for mode, must_say, in_hint in [("none", "не найден", "Отладка по USB"),
                                ("unauthorized", "не готов", "Разрешить отладку"),
                                ("offline", "не готов", "кабел"),
                                ("multiple", "несколько", "оставьте")]:
    set_mode(mode)
    try:
        usb.pick_device()
        check("режим {} даёт ошибку".format(mode), False)
    except usb.UsbError as e:
        check("{} -> «{}»".format(mode, must_say),
              must_say in str(e) and in_hint in e.hint, e.full())

set_mode("portbusy")
try:
    usb.ensure_forward(port_gw)
    check("занятый порт даёт ошибку", False)
except usb.UsbError as e:
    check("занятый порт -> сменить порт", "порт занят" in e.hint, e.full())

set_mode("ready")
device = usb.ensure_forward(port_gw, 8080)
check("проброс поставлен", (port_gw, 8080) in usb.forwards(), usb.forwards())
check("status показывает проброс", usb.status(port_gw)["forwarded"])

db.save_settings({"connection_mode": "usb", "usb_local_port": str(port_gw),
                  "gateway_host": "192.168.1.50", "gateway_password": "secret"})
client = gw.from_settings(db.get_settings())
check("в режиме USB адрес — localhost", client.host == "127.0.0.1" and client.port == port_gw,
      (client.host, client.port))
check("health через проброс работает", client.health()[0])

try:
    gw.Gateway("127.0.0.1", dead, "sms", "secret", timeout=3, mode="usb").send("+7900", "t")
    check("мёртвый проброс даёт ошибку", False)
except gw.GatewayError as e:
    check("в режиме USB подсказка про кабель", "проброс" in e.hint and "USB" in e.hint, e.hint)

print("\n=== Запуск шлюза на телефоне с компьютера ===")
set_mode("ready")
open(CALLS, "w").close()
check("шлюз опознан как установленный", usb.is_gateway_installed())
check("версия прочитана", usb.gateway_version() == "1.70.4", usb.gateway_version())

usb.start_gateway()
calls = open(CALLS, encoding="utf-8").read()
check("приложение выводится на передний план",
      "am start -n me.capcom.smsgateway/.MainActivity" in calls, calls)
check("отправляется незащищённое «загрузочное» событие",
      "QUICKBOOT_POWERON" in calls and ".receivers.BootReceiver" in calls, calls)
check("настоящее BOOT_COMPLETED не шлём (система его запрещает)",
      "android.intent.action.BOOT_COMPLETED" not in calls)

granted, failed = usb.grant_permissions()
check("выдаются оба разрешения", granted == ["SEND_SMS", "READ_PHONE_STATE"],
      (granted, failed))
check("снимается ограничение батареи", usb.allow_background())

state = usb.phone_state()
check("состояние телефона собрано",
      state["installed"] and state["version"] == "1.70.4"
      and "SEND_SMS" in state["permissions"] and state["battery_exempt"], state)

set_mode("nogateway")
try:
    usb.start_gateway()
    check("без приложения — ошибка", False, "исключения не было")
except usb.UsbError as exc:
    check("без приложения объясняет, что ставить",
          "не установлено" in str(exc) and "SMS Gateway" in exc.hint, exc.full())

set_mode("nogrant")
granted, failed = usb.grant_permissions()
check("неудачная выдача разрешений не роняет, а сообщается",
      granted == [] and len(failed) == 2, (granted, failed))
set_mode("ready")

print("\n=== Маршруты панели ===")
webapp.app.config["TESTING"] = True
web = webapp.app.test_client()
set_mode("none")
data = web.post("/settings/usb-connect").get_json()
check("без телефона — отказ, а не 500", data["ok"] is False and "не найден" in data["note"])
set_mode("ready")
data = web.post("/settings/usb-connect").get_json()
check("подключение по USB успешно", data["ok"], data)
check("видна модель телефона", "SM A515F" in data["note"], data["note"])
check("режим сохранён", db.get_settings()["connection_mode"] == "usb")
check("/settings/usb-status показывает проброс",
      web.post("/settings/usb-status").get_json()["forwarded"])
check("/settings/discover отвечает", "candidates" in web.post("/settings/discover").get_json())

print("\n" + "=" * 55)
print("ПРОВАЛЕНО {}: {}".format(len(FAILS), "; ".join(FAILS)) if FAILS
      else "Все проверки пройдены.")
sys.exit(1 if FAILS else 0)
