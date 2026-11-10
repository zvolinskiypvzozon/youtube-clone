#!/usr/bin/env python3
"""Сквозная проверка развёрнутого стека через API: регистрация → загрузка → обработка →
просмотр → приватность и управление (допуск, удаление).

Запуск из каталога репозитория (нужен доступ к docker — тестовое видео генерирует ffmpeg
из контейнера worker, на хосте ffmpeg не нужен):

    sudo python3 scripts/smoke.py                 # http://localhost
    sudo python3 scripts/smoke.py http://<IP>     # другой адрес

Только стандартная библиотека Python 3. Код выхода 0 — всё прошло.

Сценарий из ТЗ «регистрация → загрузка → просмотр → лайк и комментарий»: лайк и комментарий
добавятся с их API (день 5). Состояние видео читается только через API (карточка владельца),
без доступа к БД.
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


# Метка прогона в названии видео: поиск находит ровно это видео, а не остатки прошлых прогонов.
RUN = secrets.token_hex(4)


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


def compose(*args: str) -> bytes:
    result = subprocess.run(
        ["docker", "compose", *args], cwd=REPO, capture_output=True, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode(errors="replace").strip()[-300:])
    return result.stdout


@step("API отвечает (/api/health)")
def check_health() -> None:
    status, _, body = http("GET", "/api/health")
    expect(status, 200, body)
    if json.loads(body).get("status") != "ok":
        raise AssertionError(body.decode())


def new_user() -> dict:
    """Регистрирует пользователя: id, ник и Bearer-заголовок."""
    name = f"smoke_{secrets.token_hex(4)}"
    password = secrets.token_urlsafe(12)
    creds = {"email": f"{name}@example.com", "username": name, "password": password}
    status, _, body = http("POST", "/api/auth/register", data=creds)
    expect(status, 201, body)
    user_id = json.loads(body)["id"]
    status, _, body = http("POST", "/api/auth/token", form={"username": name, "password": password})
    expect(status, 200, body)
    auth = {"Authorization": f"Bearer {json.loads(body)['access_token']}"}
    return {"id": user_id, "username": name, "auth": auth}


@step("Регистрация и вход")
def register() -> dict:
    return new_user()


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
        "title": f"Smoke test {RUN}",
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


@step("Обработка worker-ом до статуса ready (карточка владельца)")
def wait_ready(owner: dict, video_id: str) -> None:
    deadline = time.monotonic() + PROCESSING_TIMEOUT
    while time.monotonic() < deadline:
        status, _, body = http("GET", f"/api/videos/{video_id}", headers=owner["auth"])
        if status == 503:  # строка занята worker-ом (lock_timeout) — повторить
            time.sleep(2)
            continue
        expect(status, 200, body)
        state = json.loads(body)["state"]
        if state is None:
            raise AssertionError("владелец не получил служебное состояние видео")
        if state["status"] == "ready":
            return
        if state["status"] == "failed":
            raise AssertionError(f"видео failed: {state['failure_reason']}")
        time.sleep(2)
    raise AssertionError(f"не готово за {PROCESSING_TIMEOUT} с")


@step("Результаты: превью и длительность в карточке, «Мои видео»")
def check_card(owner: dict, video_id: str) -> None:
    status, _, body = http("GET", f"/api/videos/{video_id}")  # без входа: видео публичное
    expect(status, 200, body)
    card = json.loads(body)
    if card["state"] is not None:
        raise AssertionError("служебное состояние видно анониму")
    if card["duration_seconds"] != 5:
        raise AssertionError(f"длительность {card['duration_seconds']}")
    if not card["thumbnail_url"]:
        raise AssertionError("нет превью")
    status, headers, body = http("GET", card["thumbnail_url"])
    expect(status, 200, body)
    if not body.startswith(b"\xff\xd8") or header(headers, "Content-Type") != "image/jpeg":
        raise AssertionError(f"превью не JPEG: {header(headers, 'Content-Type')}")
    status, _, body = http("GET", "/api/me/videos", headers=owner["auth"])
    expect(status, 200, body)
    if [i["id"] for i in json.loads(body)["items"]] != [video_id]:
        raise AssertionError("видео нет в «Моих видео»")


def header(headers: dict, name: str) -> str | None:
    """Заголовки без учёта регистра (HTTP/1.1 nginx отдаёт как угодно)."""
    return next((v for k, v in headers.items() if k.lower() == name.lower()), None)


@step("Лента и поток: видео в GET /api/videos, Range 206, подпись проверяется")
def check_playback(video_id: str) -> str:
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
    return url


@step("Лайк, комментарий, подписка, поиск")
def check_interactions(owner: dict, video_id: str) -> None:
    fan = new_user()
    video = f"/api/videos/{video_id}"

    status, _, body = http("PUT", f"{video}/reaction", data={"kind": "like"}, headers=fan["auth"])
    expect(status, 200, body)
    if json.loads(body) != {"my_reaction": "like", "likes_count": 1, "dislikes_count": 0}:
        raise AssertionError(f"реакция: {body[:200]!r}")

    text = "<b>Отличное</b> видео"  # хранится и отдаётся как текст
    status, _, body = http("POST", f"{video}/comments", data={"body": text}, headers=fan["auth"])
    expect(status, 201, body)
    comment_id = json.loads(body)["id"]
    status, _, body = http("GET", f"{video}/comments")
    expect(status, 200, body)
    if [c["body"] for c in json.loads(body)["items"]] != [text]:
        raise AssertionError(f"комментарии: {body[:200]!r}")
    status, _, body = http("GET", video)
    card = json.loads(body)
    if (card["likes_count"], card["comments_count"]) != (1, 1):
        raise AssertionError(f"счётчики карточки: {card['likes_count']}, {card['comments_count']}")
    # Владелец видео удаляет комментарий под своим видео.
    status, _, body = http("DELETE", f"/api/comments/{comment_id}", headers=owner["auth"])
    expect(status, 204, body)
    if json.loads(http("GET", video)[2])["comments_count"] != 0:
        raise AssertionError("счётчик комментариев после удаления")

    channel = f"/api/channels/{owner['username']}"
    status, _, body = http("PUT", f"{channel}/subscription", headers=fan["auth"])
    expect(status, 200, body)
    if json.loads(body) != {"subscribed": True, "subscribers_count": 1}:
        raise AssertionError(f"подписка: {body[:200]!r}")
    status, _, body = http("PUT", f"{channel}/subscription", headers=owner["auth"])
    expect(status, 400, body)  # на свой канал нельзя
    status, _, body = http("GET", channel, headers=fan["auth"])
    expect(status, 200, body)
    if json.loads(body)["subscribed"] is not True:
        raise AssertionError("в карточке канала нет subscribed")
    status, _, body = http("GET", "/api/me/subscriptions", headers=fan["auth"])
    expect(status, 200, body)
    if [i["channel"]["handle"] for i in json.loads(body)["items"]] != [owner["username"]]:
        raise AssertionError(f"мои подписки: {body[:200]!r}")

    query = urllib.parse.urlencode({"q": f"TEST {RUN}"})  # регистр не важен
    status, _, body = http("GET", f"/api/videos?{query}")
    expect(status, 200, body)
    if [i["id"] for i in json.loads(body)["items"]] != [video_id]:
        raise AssertionError(f"поиск: {body[:200]!r}")
    status, _, body = http("GET", "/api/videos?q=%25")  # `%` — буквальный символ, не шаблон
    expect(status, 200, body)
    if video_id in {i["id"] for i in json.loads(body)["items"]}:
        raise AssertionError("поиск по `%` нашёл видео без `%` в названии")


@step("Хранилище закрыто: без подписи 403")
def check_storage_closed(video_id: str) -> None:
    for path in (f"/yt-raw/raw/{video_id}/source", f"/yt-media/videos/{video_id}/"):
        status, _, body = http("GET", path)
        expect(status, 403, body)


@step("Приватность: чужому и анониму 404, допуск открывает, отзыв закрывает")
def check_privacy(owner: dict, video_id: str) -> None:
    other = new_user()
    status, _, body = http(
        "PATCH", f"/api/videos/{video_id}", data={"visibility": "private"}, headers=owner["auth"]
    )
    expect(status, 200, body)
    if json.loads(body)["state"]["visibility"] != "private":
        raise AssertionError("PATCH не сделал видео приватным")

    def opens(headers: dict) -> bool:
        card, _, card_body = http("GET", f"/api/videos/{video_id}", headers=headers)
        stream, _, _ = http("GET", f"/api/videos/{video_id}/stream", headers=headers)
        if card != stream or card not in (200, 404):
            raise AssertionError(f"карточка {card}, поток {stream}: {card_body[:200]!r}")
        return card == 200

    if not opens(owner["auth"]):
        raise AssertionError("владелец не открыл своё приватное видео")
    if opens({}) or opens(other["auth"]):
        raise AssertionError("приватное видео открылось без допуска")
    status, _, body = http("GET", "/api/videos")
    expect(status, 200, body)
    if video_id in {item["id"] for item in json.loads(body)["items"]}:
        raise AssertionError("приватное видео в ленте")

    access = f"/api/videos/{video_id}/access"
    grant = {"username": other["username"]}
    status, _, body = http("POST", access, data=grant, headers=owner["auth"])
    expect(status, 201, body)
    if not opens(other["auth"]):
        raise AssertionError("допуск не открыл видео")
    status, _, body = http("DELETE", f"{access}/{other['id']}", headers=owner["auth"])
    expect(status, 204, body)
    if opens(other["auth"]):
        raise AssertionError("после отзыва видео открывается")


@step("Удаление: 204, затем 404, пустые «Мои видео», файла в хранилище нет")
def check_delete(owner: dict, video_id: str, media_url: str) -> None:
    status, _, body = http("DELETE", f"/api/videos/{video_id}", headers=owner["auth"])
    expect(status, 204, body)
    # Подписанная ссылка на 720p ещё действует (TTL 15 мин): объекта нет → 404 от хранилища.
    # 403 означал бы проблему подписи, а 206/200 — что файл не удалён.
    status, _, body = http("GET", media_url, headers={"Range": "bytes=0-99"})
    expect(status, 404, body)
    status, _, body = http("GET", f"/api/videos/{video_id}", headers=owner["auth"])
    expect(status, 404, body)
    status, _, body = http("GET", "/api/me/videos", headers=owner["auth"])
    expect(status, 200, body)
    if json.loads(body)["items"]:
        raise AssertionError("удалённое видео осталось в «Моих видео»")


def cleanup(owner: dict, video_id: str) -> None:
    """После провала — удалить тестовое видео, чтобы оно не осталось в ленте стека
    (best effort: на код выхода не влияет). Пользователи smoke_* остаются."""
    try:
        http("DELETE", f"/api/videos/{video_id}", headers=owner["auth"])
    except Exception as exc:  # уборка не должна маскировать провал
        print(f"  (не удалось удалить тестовое видео {video_id}: {exc})")


def main() -> int:
    print(f"Smoke-проверка {BASE}")
    started = time.monotonic()
    owner: dict | None = None
    video_id: str | None = None
    deleted = False
    try:
        check_health()
        owner = register()
        video = make_video()
        video_id = upload(owner["auth"], video)
        wait_ready(owner, video_id)
        check_card(owner, video_id)
        media_url = check_playback(video_id)
        check_interactions(owner, video_id)
        check_storage_closed(video_id)
        check_privacy(owner, video_id)
        check_delete(owner, video_id, media_url)
        deleted = True
    except SmokeError:
        print("ПРОВАЛ")
        return 1
    finally:
        if owner is not None and video_id is not None and not deleted:
            cleanup(owner, video_id)
    print(f"OK за {time.monotonic() - started:.0f} с")
    return 0


if __name__ == "__main__":
    sys.exit(main())
