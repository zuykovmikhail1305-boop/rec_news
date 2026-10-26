#!/bin/sh
# Готовит папку .env/ для compose.yml из .env сервисов: адреса localhost → имена контейнеров.
#
#   sh docker/make_env.sh           # создать недостающие файлы
#   sh docker/make_env.sh --force   # пересоздать все
#
# Ключи (REC_NEWS_LLM_API_KEY и др.) копируются как есть и не выводятся. Пароль базы — .env/db_password
# (по умолчанию rec_news; задайте свой: DB_PASSWORD=... sh docker/make_env.sh --force).
set -eu
cd "$(dirname "$0")/.."
OUT=.env
FORCE=${1:-}
PASSWORD=${DB_PASSWORD:-rec_news}
DB_URL="postgresql+psycopg://rec_news:${PASSWORD}@db:5432/rec_news"
BROKER_URL="amqp://guest:guest@rabbitmq:5672//"

mkdir -p "$OUT"

# make <файл> <исходный .env> <ключи-переопределения...>: исходный без этих ключей + новые значения
make() {
    target="$OUT/$1"; source=$2; shift 2
    if [ -f "$target" ] && [ "$FORCE" != "--force" ]; then
        echo "  · $target уже есть (пересоздать: --force)"; return
    fi
    : > "$target"
    if [ -f "$source" ]; then
        pattern=$(for pair in "$@"; do printf '%s|' "${pair%%=*}"; done)
        grep -vE "^(${pattern%|})=" "$source" | tr -d '\r' >> "$target" || true
    else
        echo "  ! нет $source — в $target только адреса контейнеров"
    fi
    printf '\n# docker compose (docker/make_env.sh)\n' >> "$target"
    for pair in "$@"; do printf '%s\n' "$pair" >> "$target"; done
    echo "  ✓ $target"
}

if [ ! -f "$OUT/db_password" ] || [ "$FORCE" = "--force" ]; then
    printf '%s' "$PASSWORD" > "$OUT/db_password"; echo "  ✓ $OUT/db_password"
fi

make ml.env services/ml/.env \
    "REC_NEWS_DATABASE_URL=$DB_URL" \
    "REC_NEWS_QDRANT_URL=http://qdrant:6333" \
    "REC_NEWS_BROKER_URL=$BROKER_URL" \
    "REC_NEWS_API_HOST=0.0.0.0" \
    "REC_NEWS_API_PORT=8002" \
    "REC_NEWS_MODELS_DIR=/usr/src/app/models"

make parser.env services/parser/.env \
    "PARSER_DATABASE_URL=$DB_URL" \
    "PARSER_BROKER_URL=$BROKER_URL" \
    "PARSER_API_HOST=0.0.0.0" \
    "PARSER_API_PORT=8001" \
    "PARSER_ML_EXPORT_ENABLED=true"

make backend.env services/backend/.env \
    "ML_API_URL=http://ml:8002" \
    "BACKEND_DATABASE_URL=$DB_URL" \
    "BACKEND_API_HOST=0.0.0.0" \
    "BACKEND_PROCESSING_ENABLED=true"
