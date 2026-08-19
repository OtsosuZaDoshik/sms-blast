#!/bin/bash
# Сборка автономного приложения из рабочего проекта.
# Исходники копируются на время сборки — рабочий проект не изменяется.
set -e
cd "$(dirname "$0")"

PROJECT="${1:-$HOME/Desktop/рассылка}"

if [ ! -f "$PROJECT/app.py" ]; then
  echo "Не нашёл проект в «$PROJECT»."
  echo "Использование: ./build.sh [путь к папке рассылка]"
  exit 1
fi

if [ ! -x .venv-build/bin/pyinstaller ]; then
  echo "Готовлю окружение сборки…"
  /usr/bin/python3 -m venv .venv-build
  .venv-build/bin/pip install --quiet --upgrade pip
  .venv-build/bin/pip install --quiet -r "$PROJECT/requirements.txt" pyinstaller
fi

echo "Копирую исходники из «$PROJECT»…"
rm -rf src build dist
mkdir -p src
cp "$PROJECT/app.py" "$PROJECT/desktop.py" src/
cp -R "$PROJECT/smsblast" src/
cp -R "$PROJECT/templates" src/
cp -R "$PROJECT/static" src/
find src -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find src -name '*.pyc' -delete 2>/dev/null || true

# Страховка: база данных хранит пароль от шлюза, в сборку она попасть не должна.
LEAKED=$(find src \( -name '*.db' -o -name '*.db-wal' -o -name '*.db-shm' -o -name '.env' \) 2>/dev/null)
if [ -n "$LEAKED" ]; then
  echo "ОШИБКА: в сборку попали файлы с данными:"
  echo "$LEAKED"
  exit 1
fi

# Собирать внутрь Desktop нельзя: он у вас в домене файл-провайдера iCloud
# Drive, который навешивает на бандл com.apple.FinderInfo. codesign такие
# «посторонние данные» отвергает, а на Apple Silicon неподписанный бинарник
# просто не запускается. Поэтому результат кладём вне синхронизируемой папки.
WORK="$HOME/.smsblast-build"
DEST="$HOME/Applications"
APP="$DEST/SMS-рассылка.app"

mkdir -p "$DEST"
rm -rf "$WORK" "$APP" "$DEST/smsblast"

echo "Собираю (результат вне iCloud: $DEST)…"
.venv-build/bin/pyinstaller --noconfirm --clean \
  --workpath "$WORK/work" --distpath "$DEST" smsblast.spec 2>&1 | tail -8

if [ -d "$APP" ]; then
  echo "Подписываю локальной подписью…"
  xattr -cr "$APP"
  codesign --force --deep --sign - "$APP" 2>&1 | sed 's/^/  /'
  if codesign --verify --deep --strict "$APP" 2>&1; then
    echo "  подпись валидна ($(codesign -dv "$APP" 2>&1 | grep -o 'Signature=.*'))"
  else
    echo "  ВНИМАНИЕ: подпись не прошла проверку, приложение может не запуститься"
  fi
fi

echo
if [ -d "$APP" ]; then
  echo "Готово:"
  echo "  приложение: $APP  ($(du -sh "$APP" | cut -f1))"
  echo "  бинарник:   $DEST/smsblast/smsblast"
  echo
  echo "Запуск:  open '$APP'"
  echo "Данные:  ~/Library/Application Support/SMS-рассылка"
elif [ -f "$DEST/smsblast.exe" ]; then
  echo "Готово: $DEST/smsblast.exe"
else
  echo "Сборка не дала ожидаемого результата — смотрите вывод выше."
  exit 1
fi
