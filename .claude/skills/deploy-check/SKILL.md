---
name: deploy-check
description: Проверка деплоя одной командой на чистой Ubuntu 24.04 (ВМ VirtualBox со снимком) и сценария «регистрация → загрузка → просмотр → лайк → комментарий». Использовать перед сдачей и после изменений в deploy.sh, compose, Dockerfile, nginx.
---

# Проверка деплоя

Проверку на ВМ выполняет человек (у агента нет доступа к ВМ) — твоя задача подготовить
инструкцию и проанализировать вывод.

## 1. Локальная предпроверка (агент)

- `bash -n deploy.sh scripts/*.sh` — синтаксис.
- Все образы закреплены по версии: `grep -n "image:" docker-compose.yml`, `grep -n "^FROM" */Dockerfile infra/*/Dockerfile` — нет `latest`.
- `docker compose config -q` — compose валиден.
- Наружу публикуется только `web`: `docker compose config | grep -A2 ports`.
- Свежий запуск: `docker compose up -d --build` → `curl -fsS localhost:$HTTP_PORT/api/health`.

## 2. Инструкция человеку (выдать текстом)

```bash
# ВМ: восстановить снимок «чистая Ubuntu 24.04», сеть — «Сетевой мост»
git clone https://github.com/zvolinskiypvzozon/youtube-clone && cd youtube-clone
time sudo ./deploy.sh
```
Попросить прислать: последние ~30 строк вывода, время выполнения, и при ошибке —
`sudo docker compose ps -a` и `sudo docker compose logs --tail=100 <сервис>`.

## 3. Критерии успеха

- Скрипт завершился с кодом 0 менее чем за ~10 минут на первом запуске.
- С хоста в браузере открывается `http://<IP ВМ>/`, `/docs`, `/api/health` → `{"status":"ok",...}`.
- Повторный `sudo ./deploy.sh` проходит без ошибок, данные сохраняются.
- `scripts/smoke.sh http://<IP ВМ>` проходит сценарий целиком (когда скрипт появится).
