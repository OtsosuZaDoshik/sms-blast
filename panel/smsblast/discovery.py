"""Поиск телефона со шлюзом в локальной сети.

Адрес телефона в обычной сети выдаёт DHCP, и после переподключения он меняется.
Вместо ручного ввода IP сканируем свою подсеть и опознаём шлюз по ответу
`GET /health`: он отдаёт JSON с releaseId/version/status и проверками вида
`battery:level`.

Важно: во время поиска логин и пароль НЕ отправляются никуда. Чужое устройство
на общем Wi-Fi не должно получить пароль от шлюза только потому, что у него
открыт тот же порт.
"""

import ipaddress
import json
import re
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import requests

# Проверки, которые встречаются только в health-ответе SMS Gateway.
GATEWAY_CHECK_PREFIXES = ("battery:", "messages:", "connection:")
MAX_SCAN_HOSTS = 1024


def local_networks(max_hosts=MAX_SCAN_HOSTS):
    """Подсети, к которым подключён компьютер. VPN и loopback пропускаем."""
    if sys.platform.startswith("win"):
        return _windows_networks(max_hosts)
    return _unix_networks(max_hosts)


def _fit(address, prefix, max_hosts):
    """Сеть по адресу и длине префикса, слишком широкую сужаем до /24."""
    try:
        network = ipaddress.ip_network("{}/{}".format(address, prefix), strict=False)
    except ValueError:
        return None
    if network.num_addresses > max_hosts:
        network = ipaddress.ip_network("{}/24".format(address), strict=False)
    return network


def _skip_address(address):
    return address.startswith(("127.", "169.254."))


# --- Windows ---------------------------------------------------------------

PS_COMMAND = ("Get-NetIPAddress -AddressFamily IPv4 | "
              "Select-Object IPAddress,PrefixLength,InterfaceAlias | ConvertTo-Json")


def _windows_networks(max_hosts=MAX_SCAN_HOSTS):
    """ipconfig переведён на язык системы, поэтому спрашиваем PowerShell:
    Get-NetIPAddress отдаёт JSON и не зависит от локали."""
    try:
        output = subprocess.check_output(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", PS_COMMAND],
            text=True, timeout=20,
        )
        networks = parse_powershell_networks(output, max_hosts)
        if networks:
            return networks
    except (subprocess.SubprocessError, OSError, ValueError):
        pass
    return _fallback_networks(max_hosts)


def parse_powershell_networks(text, max_hosts=MAX_SCAN_HOSTS):
    """Разбор вывода Get-NetIPAddress. Вынесено отдельно ради тестов."""
    data = json.loads(text)
    if isinstance(data, dict):
        data = [data]

    networks = []
    for item in data:
        address = str(item.get("IPAddress", "")).strip()
        if not address or _skip_address(address):
            continue
        alias = str(item.get("InterfaceAlias", "")).strip()
        # Виртуальные адаптеры Hyper-V/WSL/VirtualBox — телефона там нет.
        if re.search(r"(vEthernet|WSL|Loopback|VirtualBox|VMware|Hyper-V)", alias, re.I):
            continue
        try:
            prefix = int(item.get("PrefixLength", 24))
        except (TypeError, ValueError):
            prefix = 24
        network = _fit(address, prefix, max_hosts)
        if network:
            networks.append((alias or "сеть", address, network))
    return networks


def _fallback_networks(max_hosts=MAX_SCAN_HOSTS):
    """Запасной вариант: узнаём свой адрес через сокет и берём /24."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))  # пакетов не шлёт, только выбирает маршрут
        address = sock.getsockname()[0]
    except OSError:
        return []
    finally:
        sock.close()

    if _skip_address(address):
        return []
    network = _fit(address, 24, max_hosts)
    return [("сеть", address, network)] if network else []


# --- macOS и Linux ---------------------------------------------------------

def _unix_networks(max_hosts=MAX_SCAN_HOSTS):
    try:
        output = subprocess.check_output(["ifconfig"], text=True, timeout=10)
    except (subprocess.SubprocessError, OSError):
        return _fallback_networks(max_hosts)
    return parse_ifconfig_networks(output, max_hosts)


def parse_ifconfig_networks(output, max_hosts=MAX_SCAN_HOSTS):
    networks = []
    interface = ""
    for line in output.splitlines():
        header = re.match(r"^(\w+):", line)
        if header:
            interface = header.group(1)
            continue
        # VPN-туннели и loopback не содержат телефон.
        if interface.startswith(("utun", "lo", "gif", "stf", "awdl", "llw")):
            continue

        match = re.search(r"inet (\d+\.\d+\.\d+\.\d+) netmask (0x[0-9a-f]+)", line)
        if not match:
            continue

        address, netmask_hex = match.group(1), match.group(2)
        if _skip_address(address):
            continue

        prefix = bin(int(netmask_hex, 16)).count("1")
        network = _fit(address, prefix, max_hosts)
        if network:
            networks.append((interface, address, network))

    return networks


def _tcp_open(address, port, timeout):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        return sock.connect_ex((address, port)) == 0
    except OSError:
        return False
    finally:
        sock.close()


def looks_like_gateway(data):
    if not isinstance(data, dict):
        return False
    if data.get("status") in ("pass", "warn", "fail") and (
        "releaseId" in data or "version" in data
    ):
        return True
    checks = data.get("checks")
    if isinstance(checks, dict) and any(
        str(key).startswith(GATEWAY_CHECK_PREFIXES) for key in checks
    ):
        return True
    return False


def describe(data):
    """Короткая подпись устройства: версия, батарея, состояние связи."""
    parts = []
    version = data.get("version")
    if version:
        parts.append("версия {}".format(version))

    checks = data.get("checks") or {}
    battery = checks.get("battery:level") or {}
    if battery.get("observedValue") is not None:
        parts.append("батарея {}%".format(battery["observedValue"]))

    connection = checks.get("connection:status") or {}
    if connection.get("observedValue") is not None:
        parts.append("связь есть" if connection["observedValue"] else "нет связи")

    status = data.get("status")
    if status and status != "pass":
        parts.append("состояние: {}".format(status))

    return ", ".join(parts)


def _probe(address, port, http_timeout):
    """Один кандидат → что это за устройство. Без авторизации."""
    url = "http://{}:{}/health".format(address, port)
    try:
        response = requests.get(url, timeout=http_timeout)
    except requests.exceptions.RequestException:
        return {"host": address, "port": port, "kind": "other",
                "note": "порт открыт, но на /health не отвечает"}

    if response.status_code in (401, 403):
        return {"host": address, "port": port, "kind": "maybe",
                "note": "сервер требует авторизацию — возможно, это шлюз"}

    try:
        data = response.json()
    except ValueError:
        return {"host": address, "port": port, "kind": "other",
                "note": "отвечает не JSON — это не шлюз"}

    if looks_like_gateway(data):
        note = describe(data) or "шлюз опознан"
        return {"host": address, "port": port, "kind": "gateway", "note": note}

    return {"host": address, "port": port, "kind": "other",
            "note": "чужое устройство — подпись шлюза не совпала"}


def scan(port=8080, connect_timeout=0.4, http_timeout=3.0, workers=128,
         skip_addresses=()):
    """Ищет шлюз в своих подсетях.

    Возвращает (кандидаты, что_просканировано). Кандидаты отсортированы:
    опознанные шлюзы первыми.
    """
    networks = local_networks()
    if not networks:
        return [], []

    targets, scanned = [], []
    skip = set(skip_addresses)
    for interface, address, network in networks:
        skip.add(address)
        hosts = [str(h) for h in network.hosts()]
        targets.extend(hosts)
        scanned.append({
            "interface": interface,
            "network": str(network),
            "hosts": len(hosts),
        })

    targets = [t for t in dict.fromkeys(targets) if t not in skip]

    with ThreadPoolExecutor(max_workers=workers) as pool:
        open_hosts = [
            address
            for address, is_open in zip(
                targets,
                pool.map(lambda a: _tcp_open(a, port, connect_timeout), targets),
            )
            if is_open
        ]

    with ThreadPoolExecutor(max_workers=min(32, max(1, len(open_hosts)))) as pool:
        candidates = list(pool.map(lambda a: _probe(a, port, http_timeout), open_hosts)) \
            if open_hosts else []

    order = {"gateway": 0, "maybe": 1, "other": 2}
    candidates.sort(key=lambda c: (order.get(c["kind"], 3), c["host"]))
    return candidates, scanned
