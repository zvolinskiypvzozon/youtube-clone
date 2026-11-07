#!/usr/bin/env bash
# Деплой одной командой на чистой Ubuntu 24.04:
#   git clone https://github.com/zvolinskiypvzozon/youtube-clone && cd youtube-clone && sudo ./deploy.sh
#
# Идемпотентен: повторный запуск пересобирает и перезапускает стек, данные и .env сохраняются.
# Опции:
#   --no-seed              не загружать демо-данные
#   --skip-network-check   не проверять DNS и интернет перед стартом
set -euo pipefail

SELF="$(readlink -f "$0")"

SEED=1
NET_CHECK=1
for arg in "$@"; do
  case "$arg" in
    --no-seed) SEED=0 ;;
    --skip-network-check) NET_CHECK=0 ;;
    -h|--help) sed -n '2,8p' "$SELF"; exit 0 ;;
    *) echo "Неизвестный аргумент: $arg" >&2; exit 2 ;;
  esac
done

if [[ $EUID -ne 0 ]]; then
  exec sudo bash "$SELF" "$@"
fi

cd "$(dirname "$SELF")"

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31mОШИБКА:\033[0m %s\n' "$*" >&2; exit 1; }

diagnostics() {
  docker compose ps -a || true
  docker compose logs --tail=60 || true
}

if ! grep -qi '^ID=ubuntu' /etc/os-release 2>/dev/null; then
  log "Внимание: скрипт рассчитан на Ubuntu 24.04, продолжаю на свой риск"
fi

# До любых установок: без DNS и интернета дальше всё равно упадём, но с невнятной ошибкой apt/docker.
if [[ $NET_CHECK -eq 1 ]]; then
  log "Проверяю DNS и доступ в интернет"
  bash scripts/check-network.sh || fail "нет сети или DNS (подробности выше)"
fi

# Сразу после загрузки ВМ unattended-upgrades держит блокировку dpkg — ждём её, а не падаем.
APT=(apt-get -o DPkg::Lock::Timeout=600 -qq)
export DEBIAN_FRONTEND=noninteractive

install_docker() {
  log "Устанавливаю Docker Engine и Compose plugin из официального репозитория Docker"
  "${APT[@]}" update
  "${APT[@]}" install -y ca-certificates curl openssl >/dev/null
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  # shellcheck disable=SC1091
  . /etc/os-release
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  "${APT[@]}" update
  "${APT[@]}" install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null
}

# Запуск демона: systemd на обычной Ubuntu; без systemd (например, WSL без [boot] systemd=true) — service.
start_docker() {
  if [[ -d /run/systemd/system ]]; then
    systemctl enable --now docker
  else
    log "systemd не обнаружен — запускаю Docker через service"
    service docker start
  fi
  for _ in $(seq 1 30); do
    docker info >/dev/null 2>&1 && return 0
    sleep 1
  done
  fail "демон Docker не запустился (journalctl -u docker или /var/log/docker.log)"
}

if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  install_docker
else
  log "Docker уже установлен: $(docker --version)"
fi
docker info >/dev/null 2>&1 || start_docker
if ! command -v openssl >/dev/null || ! command -v curl >/dev/null; then
  "${APT[@]}" update && "${APT[@]}" install -y openssl curl >/dev/null
fi

# Тома есть, а .env потерян: новые секреты не подойдут к существующим БД и хранилищу.
if [[ ! -f .env ]] && docker volume inspect youtube-clone_pgdata >/dev/null 2>&1; then
  fail ".env отсутствует, но данные предыдущей установки (тома youtube-clone_*) существуют.
  Восстановите .env из резервной копии или удалите данные: docker compose down -v"
fi

log "Готовлю .env"
bash scripts/gen-env.sh .env
HTTP_PORT="$(grep -E '^HTTP_PORT=' .env | cut -d= -f2 || true)"
HTTP_PORT="${HTTP_PORT:-80}"

log "Собираю и запускаю стек (первый запуск — несколько минут)"
DEPLOY_STARTED="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
if ! docker compose up -d --build --remove-orphans; then
  diagnostics
  fail "не удалось запустить стек (см. логи выше)"
fi

log "Жду готовности приложения"
deadline=$((SECONDS + 300))
until curl -fsS "http://127.0.0.1:${HTTP_PORT}/api/health" >/dev/null 2>&1; do
  if (( SECONDS > deadline )); then
    diagnostics
    fail "приложение не стало готовым за 5 минут"
  fi
  sleep 3
done

if [[ $SEED -eq 1 ]]; then
  if [[ -f scripts/seed.sh ]]; then
    log "Загружаю демо-данные"
    bash scripts/seed.sh
  else
    log "Скрипт демо-данных ещё не готов — пропускаю"
  fi
fi

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
PORT_SUFFIX=""
[[ "$HTTP_PORT" != "80" ]] && PORT_SUFFIX=":${HTTP_PORT}"
log "Готово"
cat <<EOF

  Приложение:  http://${IP:-localhost}${PORT_SUFFIX}/
  Swagger:     http://${IP:-localhost}${PORT_SUFFIX}/docs
  Health:      http://${IP:-localhost}${PORT_SUFFIX}/api/health

EOF

# Администратор. Пароль показываем, только если bootstrap создал администратора именно в этом
# запуске (маркер в логе migrate) и вывод идёт в терминал — не в лог CI/cloud-init/tee.
env_value() { grep -E "^$1=" .env | cut -d= -f2- || true; }
ADMIN_USERNAME="$(env_value ADMIN_USERNAME)"
ADMIN_CREATED=0
docker compose logs --no-log-prefix --since "$DEPLOY_STARTED" migrate 2>/dev/null \
  | grep -q "bootstrap admin: создан" && ADMIN_CREATED=1
if [[ $ADMIN_CREATED -eq 1 && -t 1 ]]; then
  cat <<EOF
  Администратор:  ${ADMIN_USERNAME}
  Пароль:         $(env_value ADMIN_PASSWORD)
  (показан один раз; хранится в .env, переменная ADMIN_PASSWORD)

EOF
else
  echo "  Администратор: ${ADMIN_USERNAME}"
  echo "  Пароль: sudo grep ADMIN_PASSWORD $(pwd)/.env"
  echo
fi

# Одноразовые контейнеры (migrate, storage-init) держат секреты в окружении
# (видны в docker inspect) — после успешного деплоя удаляем их.
docker compose rm -f migrate storage-init >/dev/null 2>&1 || true
