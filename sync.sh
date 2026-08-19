#!/bin/bash
# Обновляет репозиторий из рабочих папок на Рабочем столе.
# Копируется только код — база, .env и загрузки исключены по белому списку.
set -e
cd "$(dirname "$0")"

P="${1:-$HOME/Desktop/рассылка}"
B="${2:-$HOME/Desktop/рассылка-сборка}"

for d in "$P" "$B"; do
  [ -d "$d" ] || { echo "Не нашёл папку «$d»"; exit 1; }
done

rm -rf panel build
mkdir -p panel build

cp "$P"/*.py "$P/requirements.txt" "$P/run.sh" \
   "$P/.env.example" "$P/.gitignore" "$P/README.md" panel/
cp -R "$P/smsblast" "$P/templates" "$P/static" "$P/tests" panel/
cp "$B/launcher.py" "$B/smsblast.spec" "$B/build.sh" "$B/build-windows.bat" "$B/README.md" "$B/.gitignore" build/

find panel build -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find panel build \( -name '*.pyc' -o -name '.DS_Store' \) -delete 2>/dev/null || true

LEAK=$(find panel build \( -name '*.db' -o -name '*.db-*' -o -name '.env' -o -name '*.csv' -o -name '*.xlsx' \) 2>/dev/null)
if [ -n "$LEAK" ]; then
  echo "ОШИБКА: в репозиторий попали файлы с данными:"; echo "$LEAK"; exit 1
fi

echo "Синхронизировано. Изменения:"
git status --short
