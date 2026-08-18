"""Настройки: значения из .env — начальные, рабочие живут в БД."""

import os

from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

load_dotenv(os.path.join(BASE_DIR, ".env"))

DB_PATH = os.path.join(BASE_DIR, "smsblast.db")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
EXPORT_DIR = os.path.join(BASE_DIR, "exports")

FLASK_HOST = os.getenv("FLASK_HOST", "127.0.0.1")
# 5000 на macOS занимает «Приёмник AirPlay», поэтому по умолчанию 5001.
FLASK_PORT = int(os.getenv("PORT") or os.getenv("FLASK_PORT", "5001"))
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")

# Значения по умолчанию для таблицы settings при первом запуске.
DEFAULT_SETTINGS = {
    # wifi — по адресу телефона в сети; usb — через adb forward на 127.0.0.1.
    "connection_mode": os.getenv("CONNECTION_MODE", "wifi"),
    "usb_local_port": os.getenv("USB_LOCAL_PORT", "18080"),
    "gateway_host": os.getenv("GATEWAY_HOST", "192.168.1.50"),
    "gateway_port": os.getenv("GATEWAY_PORT", "8080"),
    "gateway_user": os.getenv("GATEWAY_USER", "sms"),
    "gateway_password": os.getenv("GATEWAY_PASSWORD", ""),
    "default_country_code": os.getenv("DEFAULT_COUNTRY_CODE", "7"),
    # Пауза между сообщениями. 4 секунды — компромисс между скоростью
    # и лимитом Android (~30 SMS за 30 минут без подтверждения).
    "delay_sec": "4",
    "jitter_sec": "2",
    "hourly_limit": "100",
    "sim_number": "0",  # 0 = SIM по умолчанию
    "max_attempts": "3",
}
