---
name: deploy-check
description: Проверка деплоя одной командой на чистой Ubuntu 24.04 (WSL-дистрибутив yt-clean со сбросом из tar-снимка; запасной вариант — ВМ VirtualBox) и сценария «регистрация → загрузка → просмотр → лайк → комментарий». Использовать перед сдачей и после изменений в deploy.sh, compose, Dockerfile, nginx.
---

# Проверка деплоя

Полная процедура описана в `docs/deploy-testing.md`, при изменении способа проверки обнови её.
Сам деплой в чистой среде запускает человек: у агента нет доступа к `yt-clean`
и к ВМ, а сброс выполняется из PowerShell на Windows. Твоя задача — подготовить среду,
выдать инструкцию и разобрать результат.

## 1. Локальная предпроверка (агент)

- `bash -n deploy.sh scripts/*.sh && sh -n infra/silo/init.sh` — синтаксис.
- Нет плавающих тегов: `grep -n "image:" docker-compose.yml`, `grep -n "^FROM" backend/Dockerfile infra/*/Dockerfile`.
- `docker compose config -q` — compose валиден.
- Наружу публикуется только `web`:
  `docker compose --profile test config --format json | python3 -c "import sys,json; print([k for k,v in json.load(sys.stdin)['services'].items() if v.get('ports')])"` → `['web']`.
- Свежий запуск локально: `docker compose up -d --build` → `curl -fsS localhost:$HTTP_PORT/api/health`.
- Изменения закоммичены, и человек сделал push: в чистой среде клонируется GitHub, а не рабочая копия.

## 2. Подготовка к прогону в `yt-clean` (агент, в рабочем дистрибутиве)

Все дистрибутивы WSL2 делят одно сетевое пространство имён. Поэтому:

1. Остановить стек проекта: `docker compose down` (**без `-v`**).
2. Сообщить человеку, что Docker в рабочем дистрибутиве нужно остановить
   (`sudo systemctl stop docker docker.socket`). Иначе два `dockerd` делят `docker0` и iptables.
   Это остановит и контейнеры других проектов, поэтому решение за человеком; сам не останавливай.
3. Проверить, что порт 80 свободен: `ss -ltn 'sport = :80'`.

## 3. Инструкция человеку (выдать текстом)

```powershell
wsl --unregister yt-clean
wsl --import yt-clean $HOME\wsl\yt-clean $HOME\wsl\yt-clean.tar
wsl -d yt-clean
```
```bash
git clone https://github.com/zvolinskiypvzozon/youtube-clone && cd youtube-clone
time sudo ./deploy.sh
```
Попросить прислать: последние ~30 строк вывода и время; при ошибке —
`sudo docker compose ps -a` и `sudo docker compose logs --tail=100 <сервис>`.

## 4. Критерии успеха

- `deploy.sh` завершился с кодом 0.
- С Windows открываются `http://localhost/`, `/docs`; `/api/health` → `{"status":"ok",...}`.
- Повторный `sudo ./deploy.sh` проходит без ошибок, данные сохраняются.
- `scripts/smoke.sh http://localhost` проходит сценарий целиком (когда скрипт появится).

## 5. Разбор результата

- Ошибку классифицируй: наш код (исправить, `fix:`-коммит, повторный прогон с чистого снимка),
  особенность среды WSL (задокументировать в `docs/deploy-testing.md`) или сеть/реестр (повторить).
- Итог занеси в отчёт дня `docs/daily/day-NN.md`.
- После прогона напомнить человеку запустить Docker обратно: `sudo systemctl start docker`.

## Запасной вариант — ВМ VirtualBox

Ubuntu 24.04 Server, «Сетевой мост», снимок `clean`. Те же команды по SSH, проверка с хоста
по `http://<IP ВМ>/`. Желательно один раз прогнать перед сдачей: WSL не проверяет сеть настоящего сервера.
