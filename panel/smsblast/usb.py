"""Подключение телефона по USB-кабелю через adb.

Адрес телефона в Wi-Fi выдаёт DHCP, и он меняется — по проводу этой проблемы
нет вовсе. `adb forward tcp:18080 tcp:8080` пробрасывает порт шлюза на
127.0.0.1 ноутбука: адрес всегда один и тот же, Wi-Fi не нужен, телефон может
сидеть на мобильном интернете и попутно заряжается.

Требуется: adb на компьютере и включённая «Отладка по USB» на телефоне.
"""

import os
import re
import subprocess

WINDOWS_CANDIDATES = [
    r"C:\platform-tools\adb.exe",
    r"C:\Program Files\Android\platform-tools\adb.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe"),
    os.path.expandvars(r"%USERPROFILE%\scoop\apps\adb\current\adb.exe"),
    r"C:\ProgramData\chocolatey\bin\adb.exe",
]

UNIX_CANDIDATES = [
    "/opt/homebrew/bin/adb",
    "/usr/local/bin/adb",
    os.path.expanduser("~/Library/Android/sdk/platform-tools/adb"),
    os.path.expanduser("~/Android/Sdk/platform-tools/adb"),
    "/Applications/Android Studio.app/Contents/plugins/android/resources/platform-tools/adb",
]


def adb_candidates():
    """Типовые места установки adb — свои на каждой ОС."""
    return WINDOWS_CANDIDATES if os.name == "nt" else UNIX_CANDIDATES


def install_hint():
    if os.name == "nt":
        return ("скачайте Android Platform Tools с developer.android.com/tools/"
                "releases/platform-tools, распакуйте и добавьте папку с adb.exe "
                "в переменную PATH")
    return "установите его командой: brew install --cask android-platform-tools"


INSTALL_HINT = install_hint()


# --- управление приложением-шлюзом на телефоне -------------------------------

GATEWAY_PACKAGE = "me.capcom.smsgateway"
GATEWAY_ACTIVITY = GATEWAY_PACKAGE + "/.MainActivity"
GATEWAY_BOOT_RECEIVER = GATEWAY_PACKAGE + "/.receivers.BootReceiver"

# BootReceiver приложения принимает несколько «загрузочных» событий. Настоящее
# BOOT_COMPLETED защищено системой и с компьютера не отправляется, а вот
# QUICKBOOT_POWERON — обычное действие, и его принять он тоже согласен. Это
# позволяет поднять шлюз, не прикасаясь к телефону.
WAKE_ACTION = "android.intent.action.QUICKBOOT_POWERON"

GATEWAY_PERMISSIONS = [
    "android.permission.SEND_SMS",
    "android.permission.READ_PHONE_STATE",
]


class UsbError(Exception):
    def __init__(self, message, hint=""):
        super().__init__(message)
        self.hint = hint

    def full(self):
        return "{} — {}".format(self, self.hint) if self.hint else str(self)


def find_adb():
    """Путь к adb или None."""
    from shutil import which

    path = which("adb")
    if path:
        return path
    for candidate in adb_candidates():
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _run(args, timeout=20):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise UsbError("adb не ответил за {} c".format(timeout),
                       hint="отключите и снова подключите кабель")
    except OSError as exc:
        raise UsbError("не удалось запустить adb: {}".format(exc), hint=install_hint())
    return result


def devices(adb=None):
    """Список устройств: [{'serial', 'state', 'model'}]."""
    adb = adb or find_adb()
    if not adb:
        raise UsbError("adb не установлен", hint=install_hint())

    result = _run([adb, "devices", "-l"])
    found = []
    for line in result.stdout.splitlines()[1:]:
        line = line.strip()
        if not line or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        model = ""
        match = re.search(r"model:(\S+)", line)
        if match:
            model = match.group(1).replace("_", " ")
        found.append({"serial": parts[0], "state": parts[1], "model": model})
    return found


def pick_device(adb=None):
    """Единственное готовое устройство или понятная ошибка."""
    found = devices(adb)
    if not found:
        raise UsbError(
            "телефон по USB не найден",
            hint="подключите кабель и включите «Отладка по USB» в разделе "
                 "«Для разработчиков» на телефоне",
        )

    ready = [d for d in found if d["state"] == "device"]
    if not ready:
        states = ", ".join("{} ({})".format(d["serial"], d["state"]) for d in found)
        unauthorized = any(d["state"] == "unauthorized" for d in found)
        raise UsbError(
            "телефон подключён, но не готов: {}".format(states),
            hint="на экране телефона подтвердите «Разрешить отладку по USB»"
                 if unauthorized else
                 "переподключите кабель и разблокируйте телефон",
        )
    if len(ready) > 1:
        raise UsbError(
            "подключено несколько устройств: {}".format(
                ", ".join(d["serial"] for d in ready)),
            hint="оставьте подключённым только телефон с приложением-шлюзом",
        )
    return ready[0]


def forwards(adb=None):
    """Действующие пробросы портов: [(локальный, удалённый)]."""
    adb = adb or find_adb()
    if not adb:
        return []
    result = _run([adb, "forward", "--list"])
    pairs = []
    for line in result.stdout.splitlines():
        match = re.search(r"tcp:(\d+)\s+tcp:(\d+)", line)
        if match:
            pairs.append((int(match.group(1)), int(match.group(2))))
    return pairs


def ensure_forward(local_port, remote_port=8080, serial=None):
    """Ставит проброс порта. Возвращает описание подключённого телефона."""
    adb = find_adb()
    if not adb:
        raise UsbError("adb не установлен", hint=install_hint())

    device = pick_device(adb)
    args = [adb]
    if serial or device["serial"]:
        args += ["-s", serial or device["serial"]]
    args += ["forward", "tcp:{}".format(int(local_port)), "tcp:{}".format(int(remote_port))]

    result = _run(args)
    if result.returncode != 0:
        error = (result.stderr or result.stdout).strip()
        hint = ""
        if "in use" in error.lower() or "cannot bind" in error.lower():
            hint = "локальный порт занят — укажите другой в настройках"
        raise UsbError("adb forward не сработал: {}".format(error or "код {}".format(
            result.returncode)), hint=hint)

    return device


def remove_forward(local_port):
    adb = find_adb()
    if not adb:
        return
    _run([adb, "forward", "--remove", "tcp:{}".format(int(local_port))])


def status(local_port=None):
    """Полная картина для интерфейса: adb, устройства, пробросы."""
    adb = find_adb()
    data = {"adb": adb or "", "devices": [], "forwards": [], "error": "",
            "forwarded": False}
    if not adb:
        data["error"] = "adb не установлен"
        data["hint"] = install_hint()
        return data

    try:
        data["devices"] = devices(adb)
        data["forwards"] = forwards(adb)
    except UsbError as exc:
        data["error"] = str(exc)
        data["hint"] = exc.hint
        return data

    if local_port:
        data["forwarded"] = any(l == int(local_port) for l, _ in data["forwards"])
    return data


def is_gateway_installed(adb=None):
    adb = adb or find_adb()
    if not adb:
        return False
    result = _run([adb, "shell", "pm", "list", "packages", GATEWAY_PACKAGE])
    return GATEWAY_PACKAGE in (result.stdout or "")


def gateway_version(adb=None):
    adb = adb or find_adb()
    if not adb:
        return ""
    result = _run([adb, "shell", "dumpsys", "package", GATEWAY_PACKAGE])
    match = re.search(r"versionName=(\S+)", result.stdout or "")
    return match.group(1) if match else ""


def start_gateway(adb=None):
    """Поднимает приложение-шлюз и его локальный сервер с компьютера.

    Сначала выводим приложение на передний план: Android запрещает запускать
    службы переднего плана из фона, а на переднем плане это разрешено. Затем
    отправляем «загрузочное» событие, по которому приложение поднимает все
    включённые в нём модули, включая локальный сервер.
    """
    adb = adb or find_adb()
    if not adb:
        raise UsbError("adb не установлен", hint=install_hint())
    if not is_gateway_installed(adb):
        raise UsbError(
            "приложение-шлюз не установлено на телефоне",
            hint="установите SMS Gateway for Android со страницы релизов "
                 "github.com/capcom6/android-sms-gateway",
        )

    _run([adb, "shell", "am", "start", "-n", GATEWAY_ACTIVITY])
    _run([adb, "shell", "am", "broadcast", "-a", WAKE_ACTION,
          "-n", GATEWAY_BOOT_RECEIVER])


def grant_permissions(adb=None):
    """Выдаёт разрешения на отправку SMS без ручных нажатий на телефоне."""
    adb = adb or find_adb()
    if not adb:
        raise UsbError("adb не установлен", hint=install_hint())

    granted, failed = [], []
    for permission in GATEWAY_PERMISSIONS:
        result = _run([adb, "shell", "pm", "grant", GATEWAY_PACKAGE, permission])
        if result.returncode == 0:
            granted.append(permission.rsplit(".", 1)[-1])
        else:
            failed.append(permission.rsplit(".", 1)[-1])
    return granted, failed


def allow_background(adb=None):
    """Исключает шлюз из экономии батареи, иначе Android усыпит его."""
    adb = adb or find_adb()
    if not adb:
        return False
    result = _run([adb, "shell", "dumpsys", "deviceidle", "whitelist",
                   "+" + GATEWAY_PACKAGE])
    return result.returncode == 0


def phone_state(adb=None):
    """Что сейчас с телефоном и приложением — для интерфейса."""
    adb = adb or find_adb()
    data = {"adb": adb or "", "device": None, "installed": False,
            "version": "", "permissions": [], "battery_exempt": False}
    if not adb:
        return data

    try:
        data["device"] = pick_device(adb)
    except UsbError:
        return data

    data["installed"] = is_gateway_installed(adb)
    if not data["installed"]:
        return data

    data["version"] = gateway_version(adb)
    dump = (_run([adb, "shell", "dumpsys", "package", GATEWAY_PACKAGE]).stdout or "")
    for permission in GATEWAY_PERMISSIONS:
        if re.search(re.escape(permission) + r": granted=true", dump):
            data["permissions"].append(permission.rsplit(".", 1)[-1])

    idle = (_run([adb, "shell", "dumpsys", "deviceidle", "whitelist"]).stdout or "")
    data["battery_exempt"] = GATEWAY_PACKAGE in idle
    return data
