#!/usr/bin/env bash
# Демо-данные для проверки: учётки (пароли — в .deploy/demo-credentials, права 600), видео,
# комментарии, лайки, подписка, плейлист, жалоба. Вызывается из deploy.sh (отключается --no-seed).
# Повторный запуск не создаёт дублей. Требует запущенный стек (docker compose up).
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."
exec python3 scripts/seed.py "$@"
