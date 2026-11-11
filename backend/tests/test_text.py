"""Пользовательский текст (день 6, шаг 1; решения архитектора 2026-11-10 и 2026-11-11).

- NUL и прочие управляющие символы C0/C1, кроме \\t \\n \\r, — 422.
- Управляющие символы направления (U+202A–U+202E, U+2066–U+2069) вырезаются до обрезки
  пробелов и проверки длины.
- Текст без единого видимого символа: в обязательных полях (название, комментарий, причина
  модерации и бана) — 422; в необязательных (описания, пояснение к жалобе) — пустое значение.
  Внутри обычного текста невидимые символы сохраняются (ZWJ в эмодзи, ZWNJ в персидском).
"""

import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import pytest
from httpx import AsyncClient, Response
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Comment, ModerationLogEntry, Playlist, Report, User, UserRole, Video
from app.schemas import text
from app.schemas.admin import MODERATION_REASON_MAX, ModerationReason
from app.schemas.moderation import REPORT_COMMENT_MAX, ReportComment
from app.schemas.playlists import (
    PLAYLIST_DESCRIPTION_MAX,
    PLAYLIST_TITLE_MAX,
    PlaylistDescription,
    PlaylistTitle,
)
from app.schemas.uploads import Description, Title
from app.schemas.videos import COMMENT_MAX, CommentBody
from tests.helpers import api_user, channel_of, new_user, seed_video

UPLOAD = {
    "title": "Тестовое видео",
    "description": "",
    "visibility": "public",
    "content_type": "video/mp4",
    "size_bytes": 1000,
}


# --- NUL в названии и описании видео (дыра: схемы видео не проверяли NUL — 500 из БД) -------


@pytest.mark.parametrize("field", ["title", "description"])
async def test_upload_nul_rejected(client: AsyncClient, field: str) -> None:
    user = await new_user(client)
    payload = {**UPLOAD, field: "a\x00b"}
    r = await client.post("/api/uploads", json=payload, headers=user["auth"])
    assert r.status_code == 422, r.text


@pytest.mark.parametrize("field", ["title", "description"])
async def test_video_update_nul_rejected(client: AsyncClient, field: str) -> None:
    user = await new_user(client)
    r = await client.post("/api/uploads", json=UPLOAD, headers=user["auth"])
    assert r.status_code == 201, r.text
    video_id = uuid.UUID(r.json()["video_id"])
    r = await client.patch(f"/api/videos/{video_id}", json={field: "a\x00b"}, headers=user["auth"])
    assert r.status_code == 422, r.text


# --- Функции модуля text ---------------------------------------------------------------------

# Текст без единого видимого символа (с пробелами по краям и без).
INVISIBLE_ONLY = [
    "\u200b",  # zero width space
    "\u200b\u200c\u200d",  # ZWSP, ZWNJ, ZWJ
    "\ufeff",  # BOM
    "\u2060\u2061\u2062\u2063\u2064",  # word joiner, невидимые операторы
    "\u00ad",  # мягкий перенос
    "\u3164",  # hangul filler
    "\u115f\u1160\uffa0",  # прочие hangul fillers
    "\u2800",  # пустой символ Брайля
    "\u00a0\u3000\u2007",  # NBSP, идеографический пробел, цифровой пробел
    "\u034f\ufe0f",  # combining grapheme joiner, вариационный селектор
    "\U000e0041\U000e007f",  # теговые символы
    "\u202e\u2066\u2069",  # одни bidi — вырезаются целиком
    " \n\t\u200b \u3164\r\n",  # смесь с обычными пробелами
    "\U000e0080\u2065\ufff0",  # неназначенные default-ignorable (Cn)
]
INVISIBLE_IDS = [
    "zwsp",
    "zw-trio",
    "bom",
    "word-joiner",
    "soft-hyphen",
    "hangul-filler",
    "fillers",
    "braille-blank",
    "spaces",
    "marks",
    "tags",
    "bidi-only",
    "mixed",
    "unassigned-ignorable",
]

BIDI_ALL = "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
FORBIDDEN_CONTROLS = ["\x00", "\x01", "\x07", "\x1b", "\x7f", "\x85", "\x9f"]
# Законные тексты с невидимыми символами внутри — сохраняются как есть.
LEGIT = [
    "👨\u200d👩\u200d👧 семья",  # ZWJ в эмодзи
    "\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645",  # ZWNJ в персидском
    "\u05e9\u05dc\u05d5\u05dd\u200f!",  # RLM в иврите
    "строка 1\nстрока 2\tтаб\r\n",
    "e\u0301",  # комбинирующий знак при букве
]


@pytest.mark.parametrize("value", INVISIBLE_ONLY, ids=INVISIBLE_IDS)
def test_invisible_only_detected(value: str) -> None:
    assert not text.has_visible(text.clean_text(value))


@pytest.mark.parametrize("value", LEGIT)
def test_legit_text_kept(value: str) -> None:
    assert text.clean_text(value) == value
    assert text.has_visible(value)


def test_bidi_controls_stripped() -> None:
    assert text.clean_text(f"a{BIDI_ALL}b\u202ec") == "abc"


@pytest.mark.parametrize(
    "ch", FORBIDDEN_CONTROLS, ids=[f"U+{ord(c):04X}" for c in FORBIDDEN_CONTROLS]
)
def test_controls_rejected(ch: str) -> None:
    with pytest.raises(ValueError):
        text.clean_text(f"a{ch}b")


def test_allowed_controls_pass() -> None:
    assert text.clean_text("a\tb\nc\rd") == "a\tb\nc\rd"


# Граница длины: bidi вырезаются до проверки длины и в неё не входят — для каждого типа
# (ловит перестановку BeforeValidator перед StringConstraints).
@pytest.mark.parametrize(
    ("annotated", "limit"),
    [
        (Title, 100),
        (Description, 5000),
        (CommentBody, COMMENT_MAX),
        (PlaylistTitle, PLAYLIST_TITLE_MAX),
        (PlaylistDescription, PLAYLIST_DESCRIPTION_MAX),
        (ReportComment, REPORT_COMMENT_MAX),
        (ModerationReason, MODERATION_REASON_MAX),
    ],
    ids=[
        "video-title",
        "video-description",
        "comment",
        "playlist-title",
        "playlist-description",
        "report-comment",
        "reason",
    ],
)
def test_length_counted_after_bidi_strip(annotated: object, limit: int) -> None:
    adapter = TypeAdapter(annotated)
    assert adapter.validate_python("\u202e" + "я" * limit + "\u202c") == "я" * limit
    with pytest.raises(ValidationError):
        adapter.validate_python("я" * (limit + 1))


# Названия однострочные: \t \n \r → пробел; в описаниях и причинах — сохраняются.
@pytest.mark.parametrize("annotated", [Title, PlaylistTitle], ids=["video-title", "playlist-title"])
def test_title_line_breaks_become_spaces(annotated: object) -> None:
    adapter = TypeAdapter(annotated)
    assert adapter.validate_python("\na\tb\nc\r\nd\u202e\n") == "a b c  d"
    with pytest.raises(ValidationError, match="at least 1"):
        adapter.validate_python("\n\t\r")


@pytest.mark.parametrize(
    "annotated",
    [Description, PlaylistDescription, ModerationReason, ReportComment, CommentBody],
    ids=["video-description", "playlist-description", "reason", "report-comment", "comment"],
)
def test_multiline_fields_keep_line_breaks(annotated: object) -> None:
    assert TypeAdapter(annotated).validate_python("a\nb\tc\r\nd") == "a\nb\tc\r\nd"


def test_huge_text_rejected_before_processing() -> None:
    """Потолок длины — до посимвольной обработки: тело около 10 МБ отклоняется быстро
    (аудит шага 1, Medium: раньше — секунды CPU в event loop на запрос)."""
    huge = "я" * 5_000_000
    adapter = TypeAdapter(Description)
    started = time.perf_counter()
    with pytest.raises(ValidationError, match="Текст слишком длинный"):
        adapter.validate_python(huge)
    assert time.perf_counter() - started < 0.5
    # Граница: ровно RAW_TEXT_MAX — до проверки длины схемы доходит (там уже 5000).
    with pytest.raises(ValidationError, match="at most 5000"):
        adapter.validate_python("я" * text.RAW_TEXT_MAX)


# --- Все точки записи текста через API ------------------------------------------------------


@dataclass
class Target:
    """Одна точка записи: send(значение) → ответ API; read() → сохранённое значение."""

    send: Callable[[str], Awaitable[Response]]
    read: Callable[[], Awaitable[str | None]]
    ok: int  # код успеха


async def _fresh(db: AsyncSession, stmt) -> object:
    await db.rollback()  # новый снимок: значение записано другой сессией (API)
    value = await db.scalar(stmt)
    await db.rollback()
    return value


async def _public_video(client: AsyncClient, db: AsyncSession) -> tuple[dict, uuid.UUID]:
    """id, а не объект: _fresh делает rollback, и объект ORM истекает (ленивая загрузка
    атрибута в повторном send — MissingGreenlet)."""
    owner = await new_user(client)
    video = await seed_video(db, await channel_of(db, owner), media=False)
    return owner, video.id


async def _comment(client: AsyncClient, db: AsyncSession) -> Target:
    _, video_id = await _public_video(client, db)
    viewer = await new_user(client)
    created: dict = {}

    async def send(v: str) -> Response:
        r = await client.post(
            f"/api/videos/{video_id}/comments", json={"body": v}, headers=viewer["auth"]
        )
        if r.status_code == 201:
            created["id"] = uuid.UUID(r.json()["id"])
        return r

    async def read() -> str | None:
        return await _fresh(db, select(Comment.body).where(Comment.id == created["id"]))

    return Target(send, read, 201)


def _upload(field: str):
    async def setup(client: AsyncClient, db: AsyncSession) -> Target:
        user = await new_user(client)
        created: dict = {}

        async def send(v: str) -> Response:
            r = await client.post("/api/uploads", json={**UPLOAD, field: v}, headers=user["auth"])
            if r.status_code == 201:
                created["id"] = uuid.UUID(r.json()["video_id"])
            return r

        async def read() -> str | None:
            col = getattr(Video, field)
            return await _fresh(db, select(col).where(Video.id == created["id"]))

        return Target(send, read, 201)

    return setup


def _video_update(field: str):
    async def setup(client: AsyncClient, db: AsyncSession) -> Target:
        owner, video_id = await _public_video(client, db)

        async def send(v: str) -> Response:
            return await client.patch(
                f"/api/videos/{video_id}", json={field: v}, headers=owner["auth"]
            )

        async def read() -> str | None:
            return await _fresh(db, select(getattr(Video, field)).where(Video.id == video_id))

        return Target(send, read, 200)

    return setup


def _playlist(field: str, *, update_existing: bool):
    async def setup(client: AsyncClient, db: AsyncSession) -> Target:
        user = await new_user(client)
        created: dict = {}
        if update_existing:
            r = await client.post(
                "/api/playlists",
                json={"title": "Плейлист", "visibility": "public"},
                headers=user["auth"],
            )
            assert r.status_code == 201, r.text
            created["id"] = uuid.UUID(r.json()["id"])

        async def send(v: str) -> Response:
            if update_existing:
                return await client.patch(
                    f"/api/playlists/{created['id']}", json={field: v}, headers=user["auth"]
                )
            payload = {"title": "Плейлист", "visibility": "public", field: v}
            r = await client.post("/api/playlists", json=payload, headers=user["auth"])
            if r.status_code == 201:
                created["id"] = uuid.UUID(r.json()["id"])
            return r

        async def read() -> str | None:
            col = getattr(Playlist, field)
            return await _fresh(db, select(col).where(Playlist.id == created["id"]))

        return Target(send, read, 200 if update_existing else 201)

    return setup


async def _report(client: AsyncClient, db: AsyncSession) -> Target:
    _, video_id = await _public_video(client, db)
    reporter = await new_user(client)
    created: dict = {}

    async def send(v: str) -> Response:
        r = await client.post(
            f"/api/videos/{video_id}/reports",
            json={"category": "spam", "comment": v},
            headers=reporter["auth"],
        )
        if r.status_code == 201:
            created["id"] = uuid.UUID(r.json()["id"])
        return r

    async def read() -> str | None:
        return await _fresh(db, select(Report.comment).where(Report.id == created["id"]))

    return Target(send, read, 201)


async def _hide(client: AsyncClient, db: AsyncSession) -> Target:
    _, video_id = await _public_video(client, db)
    moderator = await api_user(client, db, UserRole.moderator)

    async def send(v: str) -> Response:
        return await client.put(
            f"/api/admin/videos/{video_id}/moderation",
            json={"state": "hidden", "reason": v},
            headers=moderator["auth"],
        )

    async def read() -> str | None:
        return await _fresh(db, select(Video.moderation_reason).where(Video.id == video_id))

    return Target(send, read, 200)


async def _dismiss(client: AsyncClient, db: AsyncSession) -> Target:
    _, video_id = await _public_video(client, db)
    reporter = await new_user(client)
    r = await client.post(
        f"/api/videos/{video_id}/reports", json={"category": "spam"}, headers=reporter["auth"]
    )
    assert r.status_code == 201, r.text
    report_id = r.json()["id"]
    moderator = await api_user(client, db, UserRole.moderator)

    async def send(v: str) -> Response:
        return await client.post(
            f"/api/admin/reports/{report_id}/dismiss", json={"reason": v}, headers=moderator["auth"]
        )

    async def read() -> str | None:
        stmt = select(ModerationLogEntry.reason).order_by(ModerationLogEntry.created_at.desc())
        return await _fresh(db, stmt.limit(1))

    return Target(send, read, 200)


async def _ban(client: AsyncClient, db: AsyncSession) -> Target:
    target = await new_user(client)
    moderator = await api_user(client, db, UserRole.moderator)

    async def send(v: str) -> Response:
        return await client.put(
            f"/api/admin/users/{target['id']}/ban", json={"reason": v}, headers=moderator["auth"]
        )

    async def read() -> str | None:
        stmt = select(User.banned_reason).where(User.id == uuid.UUID(target["id"]))
        return await _fresh(db, stmt)

    return Target(send, read, 200)


# Обязательные поля: текст без видимых символов — 422.
REQUIRED = {
    "comment": _comment,
    "upload-title": _upload("title"),
    "video-title": _video_update("title"),
    "playlist-create-title": _playlist("title", update_existing=False),
    "playlist-update-title": _playlist("title", update_existing=True),
    "hide-reason": _hide,
    "dismiss-reason": _dismiss,
    "ban-reason": _ban,
}
# Необязательные поля: текст без видимых символов — пустое значение.
OPTIONAL = {
    "upload-description": (_upload("description"), ""),
    "video-description": (_video_update("description"), ""),
    "playlist-create-description": (_playlist("description", update_existing=False), ""),
    "playlist-update-description": (_playlist("description", update_existing=True), ""),
    "report-comment": (_report, None),
}
ALL = {**REQUIRED, **{name: setup for name, (setup, _) in OPTIONAL.items()}}

# Короткая выборка для API (полный набор — в юнит-тестах выше).
# Только пробелы и bidi (после вырезания — пустая строка) отсекает min_length с другим
# сообщением — тоже 422; здесь — случаи, которые ловит именно проверка видимости.
API_INVISIBLE = ["\u200b\u3164", "\ufeff \u2800\n", "\u202e\u00a0\u200b"]


@pytest.mark.parametrize("name", REQUIRED)
@pytest.mark.parametrize(
    "value", API_INVISIBLE, ids=["zwsp-filler", "bom-braille", "bidi-nbsp-zwsp"]
)
async def test_required_invisible_only_rejected(
    client: AsyncClient, db: AsyncSession, name: str, value: str
) -> None:
    target = await REQUIRED[name](client, db)
    r = await target.send(value)
    assert r.status_code == 422, r.text
    assert text.INVISIBLE_ONLY_MESSAGE in r.text


@pytest.mark.parametrize("name", OPTIONAL)
@pytest.mark.parametrize(
    "value", API_INVISIBLE, ids=["zwsp-filler", "bom-braille", "bidi-nbsp-zwsp"]
)
async def test_optional_invisible_only_is_blank(
    client: AsyncClient, db: AsyncSession, name: str, value: str
) -> None:
    setup, blank = OPTIONAL[name]
    target = await setup(client, db)
    r = await target.send(value)
    assert r.status_code == target.ok, r.text
    assert await target.read() == blank


# Описание из одних обычных пробелов и переводов строк теперь тоже сохраняется как ""
# (раньше — как есть); текст с видимыми символами по краям не обрезается.
@pytest.mark.parametrize("name", ["video-description", "playlist-update-description"])
async def test_description_whitespace_only_is_blank(
    client: AsyncClient, db: AsyncSession, name: str
) -> None:
    setup, _ = OPTIONAL[name]
    target = await setup(client, db)
    r = await target.send("  \n\n ")
    assert r.status_code == target.ok, r.text
    assert await target.read() == ""
    r = await target.send("  текст\n")
    assert r.status_code == target.ok, r.text
    assert await target.read() == "  текст\n"


TITLES = ["upload-title", "video-title", "playlist-create-title", "playlist-update-title"]


@pytest.mark.parametrize("name", TITLES)
async def test_title_line_breaks_stored_as_spaces(
    client: AsyncClient, db: AsyncSession, name: str
) -> None:
    target = await REQUIRED[name](client, db)
    r = await target.send("Первая\nвторая\tтретья\r\n")
    assert r.status_code == target.ok, r.text
    assert await target.read() == "Первая вторая третья"


@pytest.mark.parametrize("name", ALL)
async def test_bidi_stripped_on_write(client: AsyncClient, db: AsyncSession, name: str) -> None:
    target = await ALL[name](client, db)
    r = await target.send("Аб\u202eвг\u2066д\u2069")
    assert r.status_code == target.ok, r.text
    assert await target.read() == "Абвгд"


@pytest.mark.parametrize("name", ALL)
@pytest.mark.parametrize("ch", ["\x00", "\x1b", "\x85"], ids=["nul", "esc", "c1-nel"])
async def test_control_chars_rejected(
    client: AsyncClient, db: AsyncSession, name: str, ch: str
) -> None:
    target = await ALL[name](client, db)
    r = await target.send(f"текст{ch}текст")
    assert r.status_code == 422, r.text


@pytest.mark.parametrize("name", ALL)
async def test_legit_invisible_kept(client: AsyncClient, db: AsyncSession, name: str) -> None:
    value = "👨\u200d👩\u200d👧 \u0645\u06cc\u200c\u062e"
    target = await ALL[name](client, db)
    r = await target.send(value)
    assert r.status_code == target.ok, r.text
    assert await target.read() == value
