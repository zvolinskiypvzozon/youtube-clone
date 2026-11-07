#!/usr/bin/env bash
# Создаёт или дополняет .env случайными секретами.
# Существующие значения никогда не перезаписываются: повторный деплой не должен ломать
# доступ к уже созданным данным. Отсутствующие ключи (например, после обновления
# проекта) дописываются в конец файла.
set -euo pipefail

ENV_FILE="${1:-.env}"

rand() { openssl rand -hex "$1"; }

umask 077
if [[ ! -f "$ENV_FILE" ]]; then
  echo "# Сгенерировано scripts/gen-env.sh $(date -u +%Y-%m-%dT%H:%M:%SZ). Не коммитить." > "$ENV_FILE"
  echo "gen-env: создан $ENV_FILE (права 600)"
fi
chmod 600 "$ENV_FILE"

ensure() {
  local key="$1" value="$2"
  if ! grep -qE "^${key}=" "$ENV_FILE"; then
    echo "${key}=${value}" >> "$ENV_FILE"
    echo "gen-env: добавлен ${key}"
  fi
}

ensure APP_ENV prod
ensure LOG_LEVEL INFO
ensure HTTP_PORT 80

ensure POSTGRES_USER yt
ensure POSTGRES_PASSWORD "$(rand 24)"
ensure POSTGRES_DB yt
ensure APP_DB_USER yt_app
ensure APP_DB_PASSWORD "$(rand 24)"

ensure SECRET_KEY "$(rand 32)"

ensure STORAGE_ROOT_USER "root-$(rand 4)"
ensure STORAGE_ROOT_PASSWORD "$(rand 24)"
ensure S3_ACCESS_KEY yt-app
ensure S3_SECRET_KEY "$(rand 24)"
