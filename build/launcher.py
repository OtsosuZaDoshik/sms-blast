#!/usr/bin/env python3
"""Точка входа собранного приложения.

Отличия от запуска из исходников:
  * база, загрузки и отчёты живут в папке данных пользователя, а не рядом с
    исполняемым файлом — внутрь бандла macOS писать нельзя;
  * порт подбирается свободный, чтобы собранная версия не конфликтовала с
    панелью, запущенной из исходников;
  * браузер открывается сам — приложение запускают двойным щелчком.
"""

import os
import socket
import sys
import threading
import time

APP_NAME = "SMS-рассылка"
PREFERRED_PORT = 5001


def data_dir():
    """Папка для базы и файлов: своя на каждую ОС."""
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    elif os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    path = os.path.join(base, APP_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def exe_dir():
    """Папка, откуда запущено приложение — там ищем необязательный .env."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def bundle_dir():
    """Папка с шаблонами и статикой внутри собранного приложения."""
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def free_port(preferred=PREFERRED_PORT, attempts=25):
    for port in range(preferred, preferred + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return 0  # 0 — ядро выдаст любой свободный


def main():
    storage = data_dir()

    # В оконной сборке Windows нет stdout — без этого первый же print
    # уронил бы приложение до появления окна.
    import desktop
    desktop.ensure_output(os.path.join(storage, "app.log"))

    # .env рядом с исполняемым файлом — необязательный, но пусть работает.
    env_path = os.path.join(exe_dir(), ".env")
    if os.path.isfile(env_path):
        try:
            from dotenv import load_dotenv
            load_dotenv(env_path)
        except ImportError:
            pass

    # Пути должны быть подменены ДО импорта app: он при импорте создаёт
    # каталоги и открывает базу.
    from smsblast import config
    config.DB_PATH = os.path.join(storage, "smsblast.db")
    config.UPLOAD_DIR = os.path.join(storage, "uploads")
    config.EXPORT_DIR = os.path.join(storage, "exports")

    import app as webapp

    if getattr(sys, "frozen", False):
        # Шаблоны и статика распакованы во временную папку бандла.
        webapp.app.template_folder = os.path.join(bundle_dir(), "templates")
        webapp.app.static_folder = os.path.join(bundle_dir(), "static")

    port = free_port()
    if port == 0:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]

    print("{}: http://127.0.0.1:{}".format(APP_NAME, port))
    print("Данные: {}".format(storage))
    if port != PREFERRED_PORT:
        print("Порт {} занят, поднялись на {}".format(PREFERRED_PORT, port))

    if os.environ.get("SMSBLAST_NO_WINDOW") == "1":
        # Режим проверки сборки: сервер без окна.
        desktop.serve(port)
        while True:
            time.sleep(3600)

    return desktop.run(port)


if __name__ == "__main__":
    main()
