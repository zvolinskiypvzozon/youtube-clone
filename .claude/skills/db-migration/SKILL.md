---
name: db-migration
description: Порядок изменения схемы БД — модели SQLAlchemy, миграция Alembic, проверка upgrade/downgrade, обновление docs/db.md. Использовать при любом изменении моделей.
---

# Изменение схемы БД

1. Измени модели в `backend/app/models/`. Правила:
   - PK — `uuid` (`gen_random_uuid()`), время — `timestamptz` с `server_default=now()`.
   - Перечисления — Postgres ENUM через `sqlalchemy.Enum(..., name="...")`.
   - FK с явным `ondelete` (CASCADE для зависимых данных, RESTRICT/SET NULL — осознанно).
   - Индексы под реальные запросы (лента: `(status, visibility, published_at desc)`; поиск — trigram GIN).
   - Ограничения целостности в БД (`CheckConstraint`, `UniqueConstraint`), а не только в коде.
2. Сгенерируй миграцию в контейнере (нужна живая БД):
   ```bash
   docker compose run --rm -v "$PWD/backend:/app" --user "$(id -u)" migrate \
     alembic revision --autogenerate -m "<краткое описание>"
   ```
3. **Прочитай миграцию глазами.** Autogenerate не видит: ENUM-типы при удалении, расширения
   (`pg_trgm`, `citext`), server_default у существующих строк, переименования (видит как drop+add!).
   Допиши вручную. Расширения — `op.execute("CREATE EXTENSION IF NOT EXISTS ...")`.
4. Проверь на чистой БД: `docker compose down` (без `-v` — спроси человека!) или временная БД,
   затем `alembic upgrade head`, `alembic downgrade -1`, снова `upgrade head`.
5. Обнови `docs/db.md` (таблица + ER-диаграмма mermaid) и тесты, если поведение изменилось.
