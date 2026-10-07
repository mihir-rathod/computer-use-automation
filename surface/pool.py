"""A pool of Chromium workers, replacing "a throwaway thread per run".

Why it exists: Playwright's sync API is bound to the thread that started it. Running a replay on a shared
API worker thread leaked Playwright's event-loop state into the next, unrelated request on that thread
(WRITEUP incident 2), so each run got its own disposable thread and its own browser launch. That was safe but
launched Chromium for every run and had no bound on how many ran at once.

Here a fixed number of worker threads each own one Playwright driver and one Chromium process, for their whole
life. A job gets a fresh `BrowserContext` (its own cookies, storage and session, so concurrent runs cannot see
each other's login) and is only ever executed on the worker thread that owns the browser. At most `size` jobs
run at once; the rest wait in a queue. A crashed browser is relaunched before the next job.

Limits, stated plainly: headed or slow-mo runs are not pooled (those are launch options, and a headed run is
left open for a human), a job that blocks forever keeps its worker, and a paused escalation holds its worker
until a human resumes it.
"""
from __future__ import annotations

import atexit
import contextvars
import os
import queue
import threading
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any, TypeVar

from playwright.sync_api import Browser, Page, sync_playwright

T = TypeVar("T")
_STOP = object()


class PoolClosed(RuntimeError):
    pass


class _Worker(threading.Thread):
    def __init__(self, pool: BrowserPool, index: int):
        super().__init__(name=f"browser-worker-{index}", daemon=True)
        self.pool = pool
        self._playwright: Any = None
        self._browser: Browser | None = None
        self.launches = 0

    def _ensure_browser(self) -> Browser:
        if self._browser is None or not self._browser.is_connected():
            if self._browser is not None:
                self.pool._count("relaunched")
            self._browser = self._playwright.chromium.launch(headless=True)
            self.launches += 1
        return self._browser

    def run(self) -> None:
        self._playwright = sync_playwright().start()
        try:
            while True:
                item = self.pool._jobs.get()
                if item is _STOP:
                    return
                fn, future, default_timeout_ms = item
                if not future.set_running_or_notify_cancel():
                    continue
                self.pool._enter()
                context, result, error = None, None, None
                try:
                    context = self._ensure_browser().new_context()
                    page = context.new_page()
                    if default_timeout_ms is not None:
                        page.set_default_timeout(default_timeout_ms)
                    result = fn(page)
                except BaseException as exc:  # noqa: BLE001 -- handed to the caller through the future
                    error = exc
                try:
                    if context is not None:
                        context.close()
                except Exception:
                    pass  # the browser died under the job; _ensure_browser relaunches on the next one
                # bookkeeping first, then wake the caller, so stats() is already current when result() returns
                self.pool._leave()
                future.set_exception(error) if error is not None else future.set_result(result)
        finally:
            try:
                if self._browser is not None:
                    self._browser.close()
                self._playwright.stop()
            except Exception:
                pass


class BrowserPool:
    def __init__(self, size: int = 2):
        if size < 1:
            raise ValueError("pool size must be at least 1")
        self.size = size
        self._jobs: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._active = 0
        self._counts = {"completed": 0, "relaunched": 0, "peak_active": 0}
        self._closed = False
        self._workers = [_Worker(self, i) for i in range(size)]
        for worker in self._workers:
            worker.start()

    # ---- accounting ---------------------------------------------------------------------------

    def _count(self, key: str) -> None:
        with self._lock:
            self._counts[key] += 1

    def _enter(self) -> None:
        with self._lock:
            self._active += 1
            self._counts["peak_active"] = max(self._counts["peak_active"], self._active)

    def _leave(self) -> None:
        with self._lock:
            self._active -= 1
            self._counts["completed"] += 1

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"size": self.size, "active": self._active, "queued": self._jobs.qsize(), **self._counts}

    # ---- use ----------------------------------------------------------------------------------

    def submit(self, fn: Callable[[Page], T], default_timeout_ms: int | None = None) -> Future[T]:
        """Runs `fn(page)` on a worker thread with a fresh isolated page; `fn` must not let the page escape."""
        if self._closed:
            raise PoolClosed("the browser pool is shut down")
        future: Future[T] = Future()
        ctx = contextvars.copy_context()  # so log lines written on the worker thread still carry the caller's run id
        self._jobs.put((lambda page: ctx.run(fn, page), future, default_timeout_ms))
        return future

    def run(self, fn: Callable[[Page], T], default_timeout_ms: int | None = None) -> T:
        return self.submit(fn, default_timeout_ms).result()

    def shutdown(self, wait: bool = True) -> None:
        if self._closed:
            return
        self._closed = True
        for _ in self._workers:
            self._jobs.put(_STOP)
        if wait:
            for worker in self._workers:
                worker.join(timeout=10)


_pool: BrowserPool | None = None
_pool_lock = threading.Lock()


def get_pool() -> BrowserPool:
    """The process-wide pool, created on first use. BROWSER_POOL_SIZE sets its size (default 2)."""
    global _pool
    with _pool_lock:
        if _pool is None or _pool._closed:
            _pool = BrowserPool(int(os.environ.get("BROWSER_POOL_SIZE", "2")))
            atexit.register(_pool.shutdown, False)
        return _pool
