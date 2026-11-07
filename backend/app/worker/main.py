"""Фоновый обработчик видео (ADR 0003).

Цикл: периодические задачи (раз в WORKER_MAINTENANCE_SECONDS) → захват задачи из очереди
(Postgres, FOR UPDATE SKIP LOCKED) → обработка → снова. Нет задач — ожидание WORKER_POLL_SECONDS.

SIGTERM: текущая обработка отменяется, задача сразу возвращается в очередь (не ждём
истечения lease), процесс завершается.

При старте:
  - PR_SET_DUMPABLE=0: /proc/<pid>/environ и /mem worker-а недоступны дочернему ffmpeg того же
    uid — секреты (БД, S3) не утекут даже при выполнении кода через уязвимость декодера;
  - удаляются временные каталоги задач, оставшиеся после SIGKILL/OOM (до 2 ГБ каждый).
"""

import asyncio
import contextlib
import ctypes
import logging
import shutil
import signal
import tempfile
import time
from pathlib import Path

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.core.logging import setup_logging
from app.repositories import jobs as jobs_repo
from app.worker.maintenance import run_maintenance
from app.worker.processor import TMP_PREFIX, Outcome, process

log = logging.getLogger("worker")
PR_SET_DUMPABLE = 4
PR_GET_DUMPABLE = 3


def set_non_dumpable() -> bool:
    """Закрыть /proc/self/environ и /proc/self/mem от процессов того же uid (Linux)."""
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        return libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) == 0
    except OSError:
        return False


def is_dumpable() -> bool:
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    return libc.prctl(PR_GET_DUMPABLE, 0, 0, 0, 0) == 1


def cleanup_stale_tmp(base: Path | None = None) -> int:
    """Удаляет временные каталоги задач, оставшиеся после аварийного завершения.
    Вызывается при старте, когда этот процесс ещё ничего не обрабатывает."""
    base = base or Path(tempfile.gettempdir())
    removed = 0
    for path in base.glob(f"{TMP_PREFIX}*"):
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
    return removed


async def run_once() -> Outcome | None:
    """Захватывает и обрабатывает одну задачу. None — очередь пуста."""
    async with get_sessionmaker()() as db:
        claimed = await jobs_repo.claim(db, get_settings().worker_lease_seconds)
        await db.commit()
    if claimed is None:
        return None
    log.info("job=%s video=%s: попытка %d/%d", claimed.job_id, claimed.video_id,
             claimed.attempts, claimed.max_attempts)  # fmt: skip
    return await process(claimed)


async def run(stop: asyncio.Event) -> None:
    settings = get_settings()
    log.info("worker started")
    next_maintenance = 0.0
    while not stop.is_set():
        if time.monotonic() >= next_maintenance:
            try:
                await run_maintenance()
            except Exception:
                log.exception("maintenance: сбой")
            next_maintenance = time.monotonic() + settings.worker_maintenance_seconds

        work = asyncio.create_task(run_once())
        stopper = asyncio.create_task(stop.wait())
        done, _ = await asyncio.wait({work, stopper}, return_when=asyncio.FIRST_COMPLETED)
        if work not in done:  # пришёл SIGTERM посреди обработки
            work.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await work
            break
        stopper.cancel()
        try:
            outcome = work.result()
        except Exception:
            log.exception("worker: сбой цикла")
            outcome = None
        if outcome is None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=settings.worker_poll_seconds)
    log.info("worker stopped")


def main() -> None:
    setup_logging(get_settings().log_level)
    if not set_non_dumpable():
        log.warning("PR_SET_DUMPABLE недоступен: секреты процесса читаемы дочерними процессами")
    removed = cleanup_stale_tmp()
    if removed:
        log.info("удалено временных каталогов от прошлых запусков: %d", removed)
    loop = asyncio.new_event_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    loop.run_until_complete(run(stop))


if __name__ == "__main__":
    main()
