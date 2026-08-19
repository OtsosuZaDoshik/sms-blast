#!/usr/bin/env python3
"""Десктопная оболочка: нативное окно вместо браузера.

Flask поднимается на свободном порту в фоновом потоке, а окно рисует
системный движок — WKWebView на macOS, WebView2 на Windows. Никакого
браузера, адресной строки и вкладок: обычное приложение с иконкой в Dock.
"""

import os
import socket
import sys
import threading
import time

WINDOW_TITLE = "SMS-рассылка"
PREFERRED_PORT = 5001


def ensure_output(log_path=None):
    """В оконной сборке Windows stdout равен None, и обычный print падает
    с AttributeError. Перенаправляем вывод в файл журнала."""
    if sys.stdout is not None and sys.stderr is not None:
        return None
    target = open(log_path, "a", encoding="utf-8", errors="replace") \
        if log_path else open(os.devnull, "w")
    if sys.stdout is None:
        sys.stdout = target
    if sys.stderr is None:
        sys.stderr = target
    return target


def free_port(preferred=PREFERRED_PORT, attempts=25):
    """Свободный порт: собранная версия не должна конфликтовать с рабочей."""
    for port in range(preferred, preferred + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_until_up(port, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.4)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.15)
    return False


def serve(port):
    """Flask в фоне: окно обязано жить на главном потоке."""
    import app as webapp

    thread = threading.Thread(
        target=lambda: webapp.app.run(host="127.0.0.1", port=port,
                                      threaded=True, use_reloader=False),
        name="flask",
        daemon=True,
    )
    thread.start()
    return thread


def run(port=None):
    port = port or free_port()
    serve(port)
    url = "http://127.0.0.1:{}".format(port)

    if not wait_until_up(port):
        print("Не удалось поднять сервер на {}".format(url), file=sys.stderr)
        return 1

    try:
        import webview
    except ImportError:
        # Без pywebview остаётся браузер — приложение всё равно работает.
        import webbrowser
        print("pywebview не установлен, открываю в браузере: {}".format(url))
        webbrowser.open(url)
        while True:
            time.sleep(3600)

    webview.create_window(
        WINDOW_TITLE,
        url,
        width=1180,
        height=820,
        min_size=(940, 620),
        text_select=True,
    )

    try:
        webview.start()
    except Exception as exc:                      # noqa: BLE001 — показать любую
        fail(url, exc)
        return 1
    return 0


WEBVIEW2_URL = "https://developer.microsoft.com/microsoft-edge/webview2/"


def fail(url, exc):
    """Окно не открылось. На Windows почти всегда — отсутствует WebView2."""
    if sys.platform.startswith("win"):
        text = ("Не удалось открыть окно приложения.\n\n"
                "Скорее всего не установлен Microsoft Edge WebView2 Runtime.\n"
                "Скачайте его: {}\n\n"
                "Пока можно открыть панель в браузере: {}\n\n"
                "Подробности: {}".format(WEBVIEW2_URL, url, exc))
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, text, "SMS-рассылка", 0x10)
        except Exception:
            pass
        print(text)
    else:
        print("Не удалось открыть окно: {}\nПанель доступна на {}".format(exc, url),
              file=sys.stderr)


if __name__ == "__main__":
    sys.exit(run())
