#!/bin/bash
# Запуск панели рассылки.
set -e
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "Создаю виртуальное окружение…"
  /usr/bin/python3 -m venv .venv
  .venv/bin/pip install --quiet --upgrade pip
  .venv/bin/pip install --quiet -r requirements.txt
fi

exec .venv/bin/python app.py
