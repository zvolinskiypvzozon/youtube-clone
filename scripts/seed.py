#!/usr/bin/env python3
"""Демо-данные для проверки (вызывается scripts/seed.sh из deploy.sh; отключается --no-seed).

1. Пароли демо-учёток генерируются при первом запуске и хранятся в .deploy/demo-credentials
   (каталог 700, файл 600, в .gitignore) — не в коде и не в выводе (решение архитектора дня 9).
   Повторный запуск берёт пароли из файла.
2. Учётки и роли — app.seed_users в контейнере api (пароли — через stdin, не через окружение).
3. Контент — через HTTP API, тем же путём, что у пользователя: 4 видео (ffmpeg в контейнере
   worker), настоящая загрузка и обработка, лайки, комментарии, подписка, открытый плейлист,
   одна открытая жалоба для очереди модерации. Если у демо-автора уже есть видео — контент
   не создаётся повторно.

Запуск: python3 scripts/seed.py [URL]   (по умолчанию http://localhost:$HTTP_PORT из .env)
"""

import json
import os
import secrets
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CREDENTIALS = REPO / ".deploy" / "demo-credentials"
USERS = ("demo_admin", "demo_moderator", "demo_user", "demo_author")
PROCESSING_TIMEOUT = 300


def env_port() -> str:
    try:
        for line in (REPO / ".env").read_text().splitlines():
            if line.startswith("HTTP_PORT="):
                return line.split("=", 1)[1].strip() or "80"
    except OSError:
        pass
    return "80"


BASE = (sys.argv[1] if len(sys.argv) > 1 else f"http://localhost:{env_port()}").rstrip("/")


class SeedError(Exception):
    pass


def log(message: str) -> None:
    print(f"  seed: {message}", flush=True)


# --- HTTP ---------------------------------------------------------------------------------


def http(method: str, path: str, *, data=None, headers=None, form=None) -> tuple[int, dict, bytes]:
    """Запрос с ожиданием на 429 (сид делает много записей подряд — зоны limit_req nginx)."""
    url = path if path.startswith("http") else BASE + path
    body, hdrs = None, dict(headers or {})
    if form is not None:
        body = urllib.parse.urlencode(form).encode()
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    elif isinstance(data, bytes | bytearray):
        body = bytes(data)
    elif data is not None:
        body = json.dumps(data).encode()
        hdrs["Content-Type"] = "application/json"
    for _ in range(20):
        req = urllib.request.Request(url, data=body, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            # 429 от limit_req — запрос не дошёл до API, повтор безопасен для любого метода.
            # 503 мог прийти уже после записи — повторяем только идемпотентные GET/PUT/DELETE.
            if exc.code == 429 or (exc.code == 503 and method in ("GET", "PUT", "DELETE")):
                wait = exc.headers.get("Retry-After")
                time.sleep(min(float(wait), 30) if wait and wait.isdigit() else 3)
                continue
            return exc.code, dict(exc.headers), exc.read()
    raise SeedError(f"{method} {path}: сервер долго отвечает 429/503")


def ok(result: tuple[int, dict, bytes], *wanted: int) -> dict:
    status, _, body = result
    if status not in wanted:
        raise SeedError(f"HTTP {status}, ожидался {wanted}: {body[:200]!r}")
    return json.loads(body) if body else {}


def compose(*args: str, stdin: bytes | None = None) -> bytes:
    result = subprocess.run(
        ["docker", "compose", *args], cwd=REPO, input=stdin, capture_output=True, check=False
    )
    if result.returncode != 0:
        raise SeedError(result.stderr.decode(errors="replace").strip()[-400:])
    return result.stdout


# --- учётки ------------------------------------------------------------------------------


def load_or_create_credentials() -> dict[str, str]:
    """Пароли из .deploy/demo-credentials; нет файла — сгенерировать и записать (600)."""
    if CREDENTIALS.exists():
        # Права — и для существующего файла (скопирован, создан с другим umask — ревью дня 9).
        os.chmod(CREDENTIALS.parent, 0o700)
        os.chmod(CREDENTIALS, stat.S_IRUSR | stat.S_IWUSR)
        passwords: dict[str, str] = {}
        for line in CREDENTIALS.read_text().splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] in USERS:
                passwords[parts[0]] = parts[1]
        if set(passwords) == set(USERS):
            return passwords
        log("файл паролей неполный — генерирую заново")
    passwords = {name: secrets.token_urlsafe(18) for name in USERS}
    CREDENTIALS.parent.mkdir(mode=0o700, exist_ok=True)
    os.chmod(CREDENTIALS.parent, 0o700)
    roles = {"demo_admin": "admin", "demo_moderator": "moderator"}
    lines = [
        "# Демо-учётки — ТОЛЬКО для проверки. Для реального запуска: ./deploy.sh --no-seed",
        "# ник  пароль  роль",
        *(f"{n}  {p}  {roles.get(n, 'user')}" for n, p in passwords.items()),
    ]
    # Права 600 — с момента создания (O_CREAT с mode), а не chmod после записи.
    fd = os.open(CREDENTIALS, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(CREDENTIALS, stat.S_IRUSR | stat.S_IWUSR)
    return passwords


def ensure_users(passwords: dict[str, str]) -> None:
    out = compose(
        "exec", "-T", "api", "python", "-m", "app.seed_users",
        stdin=json.dumps(passwords).encode(),
    )  # fmt: skip
    for line in out.decode().splitlines():
        log(line.removeprefix("seed users: "))


def login(username: str, password: str) -> dict[str, str]:
    data = ok(
        http("POST", "/api/auth/token", form={"username": username, "password": password}), 200
    )
    return {"Authorization": f"Bearer {data['access_token']}"}


# --- контент -----------------------------------------------------------------------------

VIDEOS = [
    # (автор, источник ffmpeg lavfi, частота звука, название, описание, видимость)
    ("demo_author", "testsrc2=size=1280x720:rate=25", 440, "Демо: настроечная таблица",
     "Первое демо-видео канала.\nВ плеере можно выбрать 360p или 720p.", "public"),
    ("demo_author", "smptebars=size=1280x720:rate=25", 523, "Демо: цветные полосы",
     "Видео для проверки жалоб и модерации.", "public"),
    ("demo_user", "mandelbrot=size=1280x720:rate=25", 330, "Демо: фрактал",
     "Видео демо-зрителя.", "public"),
    ("demo_user", "rgbtestsrc=size=1280x720:rate=25", 660, "Демо: приватное видео",
     "Видно только владельцу и staff (и тем, кому открыт доступ).", "private"),
]  # fmt: skip


def make_video(source: str, frequency: int) -> bytes:
    return compose(
        "exec", "-T", "worker", "ffmpeg", "-v", "error",
        "-f", "lavfi", "-i", source, "-f", "lavfi", "-i", f"sine=frequency={frequency}",
        "-t", "8", "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
        "-c:a", "aac", "-shortest", "-movflags", "frag_keyframe+empty_moov", "-f", "mp4", "pipe:1",
    )  # fmt: skip


def upload(auth: dict, title: str, description: str, visibility: str, video: bytes) -> str:
    meta = {
        "title": title,
        "description": description,
        "visibility": visibility,
        "content_type": "video/mp4",
        "size_bytes": len(video),
    }
    started = ok(http("POST", "/api/uploads", data=meta, headers=auth), 201)
    parts, offset = [], 0
    for part in started["parts"]:
        chunk = video[offset : offset + part["size"]]
        offset += part["size"]
        status, headers, body = http("PUT", part["url"], data=chunk)
        if status != 200:
            raise SeedError(f"часть {part['number']}: HTTP {status} {body[:200]!r}")
        etag = next(v for k, v in headers.items() if k.lower() == "etag")
        parts.append({"number": part["number"], "etag": etag})
    video_id = started["video_id"]
    ok(http("POST", f"/api/uploads/{video_id}/complete", data={"parts": parts}, headers=auth), 200)
    return video_id


def wait_ready(auth: dict, video_id: str) -> None:
    deadline = time.monotonic() + PROCESSING_TIMEOUT
    while time.monotonic() < deadline:
        state = ok(http("GET", f"/api/videos/{video_id}", headers=auth), 200)["state"]
        if state["status"] == "ready":
            return
        if state["status"] == "failed":
            raise SeedError(f"видео {video_id} не обработано: {state['failure_reason']}")
        time.sleep(2)
    raise SeedError(f"видео {video_id} не обработано за {PROCESSING_TIMEOUT} с")


PLAYLIST_TITLE = "Лучшее демо"
DEMO_PREFIX = "Демо:"


def content_complete(author: dict) -> bool:
    """Признак готового контента — плейлист, его создание — последний шаг перед жалобой.
    Признак «есть видео» не годится: после сбоя посередине догрузка не случилась бы никогда
    (ревью дня 9)."""
    items = ok(http("GET", "/api/me/playlists", headers=author), 200)["items"]
    return any(p["title"] == PLAYLIST_TITLE for p in items)


def remove_partial(auth: dict[str, dict]) -> None:
    """После прерванного прогона — удалить демо-видео (по префиксу названия) и создать заново.
    Комментарии, реакции и элементы плейлистов удаляются каскадом вместе с видео."""
    removed = 0
    for owner in ("demo_author", "demo_user"):
        items = ok(http("GET", "/api/me/videos?limit=50", headers=auth[owner]), 200)["items"]
        for video in items:
            if video["title"].startswith(DEMO_PREFIX):
                ok(http("DELETE", f"/api/videos/{video['id']}", headers=auth[owner]), 204)
                removed += 1
    if removed:
        log(f"удалено {removed} демо-видео прерванного прогона — создаю заново")


def create_content(auth: dict[str, dict]) -> None:
    ids: list[str] = []
    for owner, source, freq, title, description, visibility in VIDEOS:
        video_id = upload(auth[owner], title, description, visibility, make_video(source, freq))
        ids.append(video_id)
        log(f"загружено «{title}»")
    for (owner, *_), video_id in zip(VIDEOS, ids, strict=True):
        wait_ready(auth[owner], video_id)
    log("все видео обработаны")
    a1, a2, u1, _private = ids
    viewer, author = auth["demo_user"], auth["demo_author"]

    ok(http("PUT", f"/api/videos/{a1}/reaction", data={"kind": "like"}, headers=viewer), 200)
    ok(http("PUT", f"/api/videos/{u1}/reaction", data={"kind": "like"}, headers=author), 200)
    ok(http("PUT", f"/api/videos/{a2}/reaction", data={"kind": "dislike"}, headers=viewer), 200)
    for video_id, who, text in [
        (a1, viewer, "Отличное видео! Подписался на канал."),
        (a1, author, "Спасибо! Скоро будет больше демо-видео."),
        (u1, author, "Красивый фрактал."),
    ]:
        ok(http("POST", f"/api/videos/{video_id}/comments", data={"body": text}, headers=who), 201)
    ok(http("PUT", "/api/channels/demo_author/subscription", headers=viewer), 200)

    playlist = ok(
        http(
            "POST",
            "/api/playlists",
            data={
                "title": PLAYLIST_TITLE,
                "description": "Подборка демо-видео",
                "visibility": "public",
            },
            headers=author,
        ),
        201,
    )
    for video_id in (a1, a2):
        ok(http("PUT", f"/api/playlists/{playlist['id']}/videos/{video_id}", headers=author), 201)

    ok(
        http(
            "POST",
            f"/api/videos/{a2}/reports",
            data={"category": "other", "comment": "Демо-жалоба для проверки очереди модерации"},
            headers=viewer,
        ),
        201,
    )
    log("лайки, комментарии, подписка, плейлист и жалоба созданы")


def main() -> int:
    print(f"Демо-данные: {BASE}")
    try:
        passwords = load_or_create_credentials()
        ensure_users(passwords)
        auth = {name: login(name, passwords[name]) for name in USERS}
        if content_complete(auth["demo_author"]):
            log("демо-контент уже есть — пропускаю")
        else:
            remove_partial(auth)
            create_content(auth)
    except (SeedError, urllib.error.URLError, OSError) as exc:
        print(f"  seed: ОШИБКА: {exc}", file=sys.stderr)
        return 1
    log(f"учётки и пароли: {CREDENTIALS} (права 600)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
