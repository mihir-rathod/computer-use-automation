"""Runs prepared runs on background workers so an API call can return its run id at once.

The workers only wait for, and then drive, the browser pool (surface/pool.py); concurrency is bounded by the pool, and a run
waits in the queue (status `queued`) until a worker takes it. Each run gets a cancel event the engine checks before every
action, so `cancel()` stops a run between actions (it cannot interrupt an action already in flight)."""
from __future__ import annotations

import os
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from runtime import Prepared


class RunExecutor:
    def __init__(self, workers: int | None = None):
        self._pool = ThreadPoolExecutor(max_workers=workers or int(os.environ.get("BROWSER_POOL_SIZE", "2")), thread_name_prefix="run-worker")
        self._cancels: dict[str, threading.Event] = {}
        self._futures: dict[str, Future] = {}
        self._lock = threading.Lock()

    def submit(self, prepared: Prepared) -> Future:
        cancel = threading.Event()
        with self._lock:
            self._cancels[prepared.run_id] = cancel

        def work() -> Any:
            try:
                return prepared.execute(cancel)
            finally:
                with self._lock:
                    self._cancels.pop(prepared.run_id, None)

        future = self._pool.submit(work)
        with self._lock:
            self._futures[prepared.run_id] = future
        return future

    def cancel(self, run_id: str) -> bool:
        with self._lock:
            event = self._cancels.get(run_id)
        if event is None:
            return False
        event.set()
        return True

    def future(self, run_id: str) -> Future | None:
        with self._lock:
            return self._futures.get(run_id)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
