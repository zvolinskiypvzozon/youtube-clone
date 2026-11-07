---
name: new-endpoint
description: Чек-лист добавления нового эндпоинта FastAPI в этом проекте (схемы, слои, политика доступа, тесты, Swagger). Использовать при создании или изменении любого API-эндпоинта.
---

# Новый эндпоинт

Выполни шаги по порядку, отмечая каждый.

1. **Контракт.** Pydantic-схемы в `backend/app/schemas/<domain>.py`: `XxxIn`, `XxxOut`.
   Ограничения полей (`max_length`, `ge`/`le`), `model_config = ConfigDict(from_attributes=True)`
   для ответов. Никогда не отдавай ORM-объект или поля вроде `password_hash` наружу.
2. **Репозиторий** `backend/app/repositories/<domain>.py`: только SQL (SQLAlchemy 2.0 select),
   без commit. Списки — с пагинацией (cursor или limit/offset с верхней границей limit ≤ 50).
3. **Сервис** `backend/app/services/<domain>.py`: бизнес-правила, транзакция (`async with session.begin()`),
   проверки прав. Если ресурс связан с видео — **только через `services/policies.py`**.
4. **Роут** `backend/app/api/routes/<domain>.py`: тонкий; `summary` на русском, `response_model`,
   `responses` для 401/403/404/409. Зависимости `current_user` / `optional_user` / `require_role(...)`.
   Подключить в `app/api/router.py`.
5. **Ошибки.** Чужой приватный ресурс → 404 (не 403). Нет авторизации → 401. Нет роли → 403.
   Конфликт → 409. Валидация → 422 (FastAPI).
6. **Тесты** `backend/tests/test_<domain>.py`: позитивный сценарий, матрица доступа, граничные
   случаи, идемпотентность для изменяющих операций.
7. **Проверка:** `docker compose --profile test run --rm tests` и ruff; открыть `/docs` — эндпоинт виден.
8. **Документация:** если изменилось поведение доступа — `docs/security.md`; если модель — `docs/db.md`.
