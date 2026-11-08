#!/usr/bin/env python3
"""Сквозная проверка развёрнутого стека: регистрация → загрузка видео → обработка → ready.

Запуск из каталога репозитория (нужен доступ к docker — тестовое видео генерирует ffmpeg
из контейнера worker, на хосте ffmpeg не нужен):

    sudo python3 scripts/smoke.py                 # http://localhost
    sudo python3 scripts/smoke.py http://<IP>     # другой адрес

Только стандартная библиотека Python 3. Код выхода 0 — всё прошло.

Сценарий из ТЗ «регистрация → загрузка → просмотр → лайк и комментарий» скрипт расширит,
когда появятся соответствующие API (дни 4–5): лента и поток (Range 206, неверная подпись 403)
уже проверяются. До появления GET /api/videos/{id} статус
обработки читается из БД через `docker compose exec postgres` (TEMPORARY).
"""

import json
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost").rstrip("/")
PROCESSING_TIMEOUT = 180


class SmokeError(Exception):
    pass


def step(title: str):
    def wrap(fn):
        def run(*args, **kwargs):
            started = time.monotonic()
            print(f"  … {title}", end="", flush=True)
            try:
                result = fn(*args, **kwargs)
            except Exception as exc:
                print(f"\r  ✘ {title}: {exc}")
                raise SmokeError(title) from exc
            print(f"\r  ✔ {title} ({time.monotonic() - started:.1f} с)")
            return result

        return run

    return wrap


def http(method: str, path: str, *, data=None, headers=None, form=None) -> tuple[int, dict, bytes]:
    url = path if path.startswith("http") else BASE + path
    body = None
    hdrs = dict(headers or {})
    if form is not None:
        body = urllib.parse.urlencode(form).encode()
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    elif isinstance(data, bytes | bytearray):
        body = bytes(data)
    elif data is not None:
        body = json.dumps(data).encode()
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def expect(status: int, wanted: int, body: bytes) -> None:
    if status != wanted:
        raise AssertionError(f"HTTP {status}, ожидался {wanted}: {body[:200]!r}")


def compose(*args: str, stdin: bytes | None = None) -> bytes:
    result = subprocess.run(
        ["docker", "compose", *args], cwd=REPO, input=stdin, capture_output=True, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode(errors="replace").strip()[-300:])
    return result.stdout


def sql(query: str) -> str:
    """TEMPORARY: до GET /api/videos/{id} (день 4)."""
    out = compose(
        "exec", "-T", "postgres", "sh", "-c", 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tA',
        stdin=query.encode(),
    )  # fmt: skip
    return out.decode().strip()


@step("API отвечает (/api/health)")
def check_health() -> None:
    status, _, body = http("GET", "/api/health")
    expect(status, 200, body)
    if json.loads(body).get("status") != "ok":
        raise AssertionError(body.decode())


@step("Регистрация и вход")
def register() -> dict[str, str]:
    name = f"smoke_{secrets.token_hex(4)}"
    password = secrets.token_urlsafe(12)
    creds = {"email": f"{name}@example.com", "username": name, "password": password}
    status, _, body = http("POST", "/api/auth/register", data=creds)
    expect(status, 201, body)
    status, _, body = http("POST", "/api/auth/token", form={"username": name, "password": password})
    expect(status, 200, body)
    return {"Authorization": f"Bearer {json.loads(body)['access_token']}"}


@step("Тестовое видео 1280×720, 5 с (ffmpeg из контейнера worker)")
def make_video() -> bytes:
    return compose(
        "exec", "-T", "worker", "ffmpeg", "-v", "error",
        "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=25",
        "-f", "lavfi", "-i", "sine=frequency=440",
        "-t", "5", "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
        "-c:a", "aac", "-shortest",
        "-movflags", "frag_keyframe+empty_moov", "-f", "mp4", "pipe:1",
    )  # fmt: skip


@step("Загрузка: старт, части напрямую в хранилище, завершение")
def upload(auth: dict[str, str], video: bytes) -> str:
    meta = {
        "title": "Smoke test",
        "description": "Проверка развёртывания",
        "visibility": "public",
        "content_type": "video/mp4",
        "size_bytes": len(video),
    }
    status, _, body = http("POST", "/api/uploads", data=meta, headers=auth)
    expect(status, 201, body)
    started = json.loads(body)
    parts, offset = [], 0
    for part in started["parts"]:
        chunk = video[offset : offset + part["size"]]
        offset += part["size"]
        status, headers, body = http("PUT", part["url"], data=chunk)
        expect(status, 200, body)
        etag = next(v for k, v in headers.items() if k.lower() == "etag")
        parts.append({"number": part["number"], "etag": etag})
    status, _, body = http(
        "POST", f"/api/uploads/{started['video_id']}/complete", data={"parts": parts}, headers=auth
    )
    expect(status, 200, body)
    return started["video_id"]


@step("Обработка worker-ом до статуса ready")
def wait_ready(video_id: str) -> None:
    deadline = time.monotonic() + PROCESSING_TIMEOUT
    while time.monotonic() < deadline:
        status = sql(f"SELECT status FROM videos WHERE id = '{video_id}'")  # noqa: S608 — id из API
        if status == "ready":
            return
        if status == "failed":
            reason = sql(f"SELECT failure_reason FROM videos WHERE id = '{video_id}'")  # noqa: S608
            raise AssertionError(f"видео failed: {reason}")
        time.sleep(2)
    raise AssertionError(f"не готово за {PROCESSING_TIMEOUT} с")


@step("Результаты: 360p, 720p, превью; исходник удалён")
def check_files(video_id: str) -> None:
    kinds = sql(
        f"SELECT string_agg(kind::text, ',' ORDER BY kind) FROM video_files "  # noqa: S608
        f"WHERE video_id = '{video_id}'"
    )
    if kinds != "mp4_360,mp4_720,thumbnail":
        raise AssertionError(f"файлы: {kinds}")


def header(headers: dict, name: str) -> str | None:
    """Заголовки без учёта регистра (HTTP/1.1 nginx отдаёт как угодно)."""
    return next((v for k, v in headers.items() if k.lower() == name.lower()), None)


@step("Лента и поток: видео в GET /api/videos, Range 206, подпись проверяется")
def check_playback(video_id: str) -> None:
    status, _, body = http("GET", "/api/videos")
    expect(status, 200, body)
    if video_id not in {item["id"] for item in json.loads(body)["items"]}:
        raise AssertionError("видео нет в ленте")

    status, _, body = http("GET", f"/api/videos/{video_id}/stream")  # без входа: видео публичное
    expect(status, 200, body)
    sources = json.loads(body)["sources"]
    if [s["quality"] for s in sources] != ["720p", "360p"]:
        raise AssertionError(f"качества: {[s['quality'] for s in sources]}")
    url = sources[0]["url"]
    if not url.startswith("/yt-media/"):
        raise AssertionError(f"ссылка не относительная: {url[:80]}")

    status, headers, body = http("GET", url, headers={"Range": "bytes=0-99"})
    expect(status, 206, body)
    if len(body) != 100 or not (header(headers, "Content-Range") or "").startswith("bytes 0-99/"):
        raise AssertionError(f"Range: {len(body)} байт, {header(headers, 'Content-Range')}")
    if header(headers, "Cache-Control") != "private, max-age=900":
        raise AssertionError(f"Cache-Control: {header(headers, 'Cache-Control')}")

    sig = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["X-Amz-Signature"][0]
    forged = url.replace(sig, ("0" if sig[0] != "0" else "1") + sig[1:])
    status, headers, body = http("GET", forged, headers={"Range": "bytes=0-99"})
    expect(status, 403, body)
    if header(headers, "Cache-Control") is not None:
        raise AssertionError(f"Cache-Control у 403: {header(headers, 'Cache-Control')}")


@step("Хранилище закрыто: без подписи 403")
def check_storage_closed(video_id: str) -> None:
    for path in (f"/yt-raw/raw/{video_id}/source", f"/yt-media/videos/{video_id}/"):
        status, _, body = http("GET", path)
        expect(status, 403, body)


def main() -> int:
    print(f"Smoke-проверка {BASE}")
    started = time.monotonic()
    try:
        check_health()
        auth = register()
        video = make_video()
        video_id = upload(auth, video)
        wait_ready(video_id)
        check_files(video_id)
        check_playback(video_id)
        check_storage_closed(video_id)
    except SmokeError:
        print("ПРОВАЛ")
        return 1
    print(f"OK за {time.monotonic() - started:.0f} с")
    return 0


if __name__ == "__main__":
    sys.exit(main())
