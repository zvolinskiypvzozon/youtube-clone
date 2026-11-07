#!/usr/bin/env bash
# Предварительная проверка сети перед деплоем: DNS и HTTPS до всех хостов, которые нужны
# для установки Docker, скачивания образов и сборки (apt, Docker Hub, PyPI, npm, Debian).
#
# DNS проверяется с повторами: в WSL бывают кратковременные сбои резолвера.
# Код выхода: 0 — сеть в порядке, 1 — есть проблемы (с понятным описанием).
set -euo pipefail

ATTEMPTS="${NET_CHECK_ATTEMPTS:-3}"

# Хосты apt берём из реальных источников системы (могут быть зеркала).
apt_hosts() {
  grep -hoE 'https?://[^/ ]+' /etc/apt/sources.list /etc/apt/sources.list.d/* 2>/dev/null \
    | sed -E 's#https?://##' | sort -u
}

HOSTS=(
  download.docker.com                 # пакеты Docker Engine
  registry-1.docker.io                # Docker Hub: манифесты
  auth.docker.io                      # Docker Hub: токены
  production.cloudflare.docker.com    # Docker Hub: слои образов
  deb.debian.org                      # apt внутри python-образа (ffmpeg)
  pypi.org files.pythonhosted.org     # pip
  registry.npmjs.org                  # npm
)
mapfile -t APT_HOSTS < <(apt_hosts || true)
[[ ${#APT_HOSTS[@]} -eq 0 ]] && APT_HOSTS=(archive.ubuntu.com)
HOSTS+=("${APT_HOSTS[@]}")

resolve() {
  local host="$1" i
  for ((i = 1; i <= ATTEMPTS; i++)); do
    getent ahosts "$host" >/dev/null 2>&1 && return 0
    sleep 2
  done
  return 1
}

failed=()
for host in "${HOSTS[@]}"; do
  resolve "$host" || failed+=("$host")
done

if [[ ${#failed[@]} -gt 0 ]]; then
  cat >&2 <<EOF
Проверка сети: не удаётся разрешить имена (DNS):
$(printf '  - %s\n' "${failed[@]}")

Деплою нужен интернет: установка Docker, образы, pip и npm.
Что проверить:
  - есть ли интернет: ping -c1 1.1.1.1
  - DNS-серверы: cat /etc/resolv.conf
  - WSL: автоматически сгенерированный resolv.conf бывает нестабилен.
    Решение описано в docs/deploy-testing.md, раздел «DNS в WSL».
Пропустить проверку (например, при локальном зеркале): ./deploy.sh --skip-network-check
EOF
  exit 1
fi

# DNS работает — проверяем, что HTTPS реально проходит (прокси, фаервол, MITM).
if command -v curl >/dev/null 2>&1; then
  for url in https://download.docker.com/ https://registry-1.docker.io/v2/; do
    # /v2/ у Docker Hub отвечает 401 без токена: для проверки связи это нормально.
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$url" || true)"
    if [[ "$code" == "000" ]]; then
      echo "Проверка сети: DNS работает, но HTTPS-соединение с $url не устанавливается" \
           "(прокси, фаервол или блокировка)." >&2
      exit 1
    fi
  done
fi

echo "Проверка сети: ok (${#HOSTS[@]} хостов)"
