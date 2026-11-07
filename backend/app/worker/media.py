"""ffprobe/ffmpeg для недоверенных файлов.

Файл загружен пользователем — это недоверенный вход для сложного парсера и декодеров.
Защиты (проверены на ffmpeg 7.1 из образа worker, тесты в tests/test_worker.py):
  - только список аргументов, без shell;
  - `-format_whitelist`: только обычные видеоконтейнеры. Плейлисты (HLS, concat) и прочие
    «форматы-ссылки» отклоняются — через них ffmpeg читает другие файлы и «рисует» их в кадрах;
  - `-codec_whitelist`: только распространённые кодеки — меньше поверхность атаки декодеров;
  - `-protocol_whitelist file`: никаких сетевых протоколов (SSRF);
  - пустое окружение ffmpeg; секреты родителя (PID 1 контейнера — worker с тем же uid)
    закрыты PR_SET_DUMPABLE=0 в worker/main.py: /proc/1/environ и /proc/1/mem недоступны
    даже при выполнении кода в ffmpeg через уязвимость декодера;
  - лимиты: разрешение, число потоков, частота кадров (ffprobe); `-t` при транскоде —
    длительность из заголовка может лгать; потоки декодера/фильтров ограничены;
  - метаданные и главы исходника не переносятся в результат (title, GPS-координаты);
  - таймауты с убийством процесса; stdout/stderr транскода не копятся в памяти.

`-enable_drefs 0` не задаётся явно: опция есть только у mov-демультиплексора и ломает
открытие других форматов; в ffmpeg 7.1 внешние data references mov выключены по умолчанию.
"""

import asyncio
import json
import os
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

# Имена демультиплексоров ffmpeg (как в `ffmpeg -demuxers`).
FORMAT_WHITELIST = "mov,mp4,m4a,3gp,3g2,mj2,matroska,webm,avi,flv,mpegts,mpeg,ogg,asf"
CODEC_WHITELIST = (
    "h264,hevc,vp8,vp9,av1,mpeg4,mpeg2video,mpeg1video,mjpeg,aac,mp3,opus,vorbis,ac3,flac,pcm_s16le"
)
SAFE_INPUT = [
    "-protocol_whitelist", "file",
    "-format_whitelist", FORMAT_WHITELIST,
    "-codec_whitelist", CODEC_WHITELIST,
]  # fmt: skip
_CLEAN_ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin"}
PROBE_TIMEOUT = 60
MAX_PIXELS = 4096 * 2160  # 4K
MAX_STREAMS = 16
MAX_FPS = 120


class MediaError(Exception):
    """Файл не является пригодным видео. Постоянная ошибка: повтор не поможет."""


@dataclass(frozen=True)
class Probe:
    duration: float
    width: int
    height: int
    has_audio: bool


async def _run(args: list[str], limit_seconds: float, *, capture: bool) -> tuple[int, bytes]:
    out = asyncio.subprocess.PIPE if capture else asyncio.subprocess.DEVNULL
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=out,
        stderr=asyncio.subprocess.DEVNULL,
        env=_CLEAN_ENV,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), limit_seconds)
    except (TimeoutError, asyncio.CancelledError):
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode or 0, stdout or b""


def _fps(value: str | None) -> float:
    try:
        return float(Fraction(value)) if value and value != "0/0" else 0.0
    except (ValueError, ZeroDivisionError):
        return 0.0


async def probe(path: Path) -> Probe:
    try:
        code, out = await _run(
            ["ffprobe", "-v", "error", *SAFE_INPUT, "-print_format", "json",
             "-show_format", "-show_streams", str(path)],
            PROBE_TIMEOUT,
            capture=True,
        )  # fmt: skip
    except TimeoutError as exc:
        raise MediaError("Файл не удалось проанализировать за отведённое время") from exc
    if code != 0:
        raise MediaError("Файл не является поддерживаемым видео")
    try:
        info = json.loads(out)
    except json.JSONDecodeError as exc:
        raise MediaError("Файл не является поддерживаемым видео") from exc

    streams = info.get("streams") or []
    if len(streams) > MAX_STREAMS:
        raise MediaError("Слишком много дорожек в файле")
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise MediaError("В файле нет видеопотока")
    try:
        duration = float(info.get("format", {}).get("duration") or video.get("duration"))
        width, height = int(video["width"]), int(video["height"])
    except (TypeError, ValueError, KeyError) as exc:
        raise MediaError("Не удалось определить длительность или размер кадра") from exc
    if duration <= 0 or width <= 0 or height <= 0:
        raise MediaError("Некорректная длительность или размер кадра")
    if width * height > MAX_PIXELS:
        raise MediaError("Слишком большое разрешение видео (максимум 4K)")
    if _fps(video.get("r_frame_rate")) > MAX_FPS:
        raise MediaError("Слишком высокая частота кадров")
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    return Probe(duration=duration, width=width, height=height, has_audio=has_audio)


def variant_heights(source_height: int) -> dict[str, int]:
    """Качества для выдачи: 360p всегда (не выше исходника), 720p — если исходник ≥ 720."""
    even = lambda h: max(2, h - h % 2)  # noqa: E731 — H.264 требует чётные размеры
    variants = {"mp4_360": even(min(360, source_height))}
    if source_height >= 720:
        variants["mp4_720"] = 720
    return variants


def transcode_timeout(duration: float) -> float:
    return max(300.0, duration * 4 + 120)


def _input(src: Path, threads: int) -> list[str]:
    # -threads перед -i ограничивает декодер (после -i — только кодировщик).
    return ["-threads", str(threads), *SAFE_INPUT, "-i", str(src)]


async def transcode(
    src: Path, dst: Path, height: int, duration: float, threads: int, max_seconds: float
) -> None:
    # -t: заголовок мог заявить 1 минуту при многочасовом потоке — выход всё равно не длиннее.
    limit = min(duration, max_seconds)
    args = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-filter_threads", str(threads),
        *_input(src, threads),
        "-map", "0:v:0", "-map", "0:a:0?",
        "-map_metadata", "-1", "-map_chapters", "-1",
        "-t", f"{limit:.3f}",
        "-vf", f"scale=-2:{height}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-ac", "2",
        "-movflags", "+faststart", "-max_muxing_queue_size", "1024",
        "-threads", str(threads),
        str(dst),
    ]  # fmt: skip
    try:
        code, _ = await _run(args, transcode_timeout(limit), capture=False)
    except TimeoutError as exc:
        raise MediaError("Обработка видео заняла слишком много времени") from exc
    if code != 0 or not dst.exists() or dst.stat().st_size == 0:
        raise MediaError("Не удалось перекодировать видео")


async def thumbnail(src: Path, dst: Path, duration: float, threads: int) -> None:
    at = f"{max(0.0, duration * 0.1):.2f}"
    args = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-filter_threads", str(threads),
        "-ss", at, *_input(src, threads),
        "-map_metadata", "-1",
        "-frames:v", "1", "-vf", "scale=640:-2", "-q:v", "3",
        str(dst),
    ]  # fmt: skip
    try:
        code, _ = await _run(args, PROBE_TIMEOUT, capture=False)
    except TimeoutError as exc:
        raise MediaError("Не удалось получить превью") from exc
    if code != 0 or not dst.exists() or dst.stat().st_size == 0:
        raise MediaError("Не удалось получить превью")


def file_size(path: Path) -> int:
    return os.path.getsize(path)
