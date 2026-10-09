"""Read offload helpers; callers provide the executor and stale-result guard."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Executor, Future


def submit_read[T](executor: Executor, read: Callable[[], T]) -> Future[T]:
    return executor.submit(read)
