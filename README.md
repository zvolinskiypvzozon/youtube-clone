# YouTube Clone

Копия YouTube на FastAPI, PostgreSQL, S3-совместимом хранилище и React. Весь код написан ИИ
(Claude Code) под руководством инженера, который выступал архитектором и ревьюером.

- Агентная обвязка лежит в репозитории: [CLAUDE.md](CLAUDE.md) и [.claude/](.claude/)
  (агенты, скиллы, хуки, settings.json).
- Диалоги с ИИ (файлы сессий Claude Code `*.jsonl` из `~/.claude/projects/<проект>/`)
  в репозиторий **не входят**. Они лежат в архиве сдачи отдельно, в исходном формате.
- Ход работы по дням: [docs/daily/](docs/daily/).

> Статус: день 1 из 10 — инфраструктура, деплой, CI, каркас API и SPA.

## Быстрый старт (чистая Ubuntu 24.04)

```bash
git clone https://github.com/zvolinskiypvzozon/youtube-clone
cd youtube-clone
sudo ./deploy.sh
```

Скрипт ставит Docker (если его нет), генерирует секреты в `.env`, собирает и запускает стек,
ждёт готовности и печатает адреса:

- приложение: `http://<IP>/`
- Swagger: `http://<IP>/docs`
- состояние: `http://<IP>/api/health`

Повторный запуск безопасен: стек обновится, данные сохранятся.

Как проверять деплой на чистой системе (WSL-дистрибутив со сбросом из снимка или ВМ):
[docs/deploy-testing.md](docs/deploy-testing.md).

## Разработка

```bash
bash scripts/gen-env.sh .env                       # один раз
docker compose up -d --build                       # стек на :80 (порт задаётся HTTP_PORT в .env)
docker compose --profile test build && docker compose --profile test run --rm tests
                                                   # pytest: отдельная БД yt_test и бакеты yt-test-*
python3 -m venv .venv && .venv/bin/pip install ruff==0.11.8   # ruff для хука Claude Code
cd frontend && npm ci && npm run dev               # SPA с hot reload, API проксируется на стек
```

## Документация

- [Архитектура и критерии приёмки](docs/architecture.md)
- [Архитектурные решения (ADR)](docs/adr/)
- [База данных](docs/db.md) · [Безопасность](docs/security.md)
- Инструкции для ИИ-агента: [CLAUDE.md](CLAUDE.md), агенты и скиллы — [.claude/](.claude/)

## Стек

FastAPI · SQLAlchemy 2 (async) · Alembic · PostgreSQL 17 · pgsty/silo (S3 API, форк MinIO) ·
ffmpeg · React 18 + Vite + TypeScript · nginx · Docker Compose · GitHub Actions
