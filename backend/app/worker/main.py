"""Фоновый обработчик видео.

День 1: каркас процесса — корректный запуск/остановка по SIGTERM и цикл опроса.
Очередь задач (Postgres, SKIP LOCKED) и ffmpeg-пайплайн появятся на дне 3.
"""

import asyncio
import contextlib
import logging
import signal

from app.core.config import get_settings
from app.core.logging import setup_logging

log = logging.getLogger("worker")

POLL_INTERVAL_SECONDS = 2.0


async def run(stop: asyncio.Event) -> None:
    log.info("worker started")
    while not stop.is_set():
        # TODO(day 3): взять задачу из очереди и обработать
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=POLL_INTERVAL_SECONDS)
    log.info("worker stopped")


def main() -> None:
    setup_logging(get_settings().log_level)
    loop = asyncio.new_event_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    loop.run_until_complete(run(stop))


if __name__ == "__main__":
    main()
