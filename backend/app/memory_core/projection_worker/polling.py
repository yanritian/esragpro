"""Polling loop for projection worker (P1-04).

Interruptible polling with idle callback support.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Callable, Awaitable

from app.memory_core.projection_worker.worker import ProjectionWorker


async def run_polling_loop(
    worker: ProjectionWorker,
    stop_event: asyncio.Event,
    idle_wait: timedelta = timedelta(seconds=1),
    idle_callback: Callable[[], Awaitable[None]] | None = None,
) -> None:
    """Polling loop with interruptible stop.

    Args:
        worker: The projection worker instance
        stop_event: asyncio.Event that interrupts idle waits when set
        idle_wait: Duration to wait between empty batches (must be positive)
        idle_callback: Optional callback invoked after idle timeout

    Raises:
        ValueError: If idle_wait is not positive
    """
    if idle_wait <= timedelta(0):
        raise ValueError("idle_wait must be positive")

    while not stop_event.is_set():
        result = await worker.run_once()

        if result.claimed == 0:
            # Use wait_for to allow stop_event to interrupt idle wait
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=idle_wait.total_seconds(),
                )
                # stop_event was set, exit loop
                break
            except asyncio.TimeoutError:
                # Idle timeout elapsed, optionally invoke callback
                if idle_callback:
                    await idle_callback()


__all__ = ["run_polling_loop"]