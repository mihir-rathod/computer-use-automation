"""The browser pool: bounded concurrency, isolated sessions, thread affinity, crash recovery."""
from __future__ import annotations

import threading
import time

import pytest

from surface.pool import BrowserPool, PoolClosed
from tests.clinic_support import sign_in


@pytest.fixture
def pool():
    p = BrowserPool(size=2)
    yield p
    p.shutdown()


def whoami(base: str, user: str, password: str, hold_s: float = 0.0):
    def job(page):
        sign_in(page, base, user, password)
        time.sleep(hold_s)  # stay open so the other worker's job overlaps
        page.goto(f"{base}/legacy/menu")
        return page.inner_text(".banner"), threading.current_thread().name
    return job


def test_concurrent_jobs_get_isolated_sessions(pool, clinic):
    """Two runs signed in as different users at the same time never see each other's session."""
    a = pool.submit(whoami(clinic, "frontdesk", "desk-demo-123", 0.6))
    b = pool.submit(whoami(clinic, "supervisor", "super-demo-123", 0.6))
    (banner_a, worker_a), (banner_b, worker_b) = a.result(timeout=60), b.result(timeout=60)

    assert "frontdesk" in banner_a and "supervisor" not in banner_a
    assert "supervisor" in banner_b and "frontdesk" not in banner_b
    assert worker_a != worker_b
    assert pool.stats()["peak_active"] == 2


def test_concurrency_is_bounded_by_pool_size(pool):
    running, peak, lock = 0, 0, threading.Lock()

    def job(page):
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        time.sleep(0.3)
        with lock:
            running -= 1
        return True

    futures = [pool.submit(job) for _ in range(6)]
    assert all(f.result(timeout=60) for f in futures)
    assert peak == 2 and pool.stats()["completed"] == 6


def test_a_job_always_runs_on_the_thread_that_owns_the_browser(pool):
    """Playwright's sync API is thread-bound; the page a job receives must be used on its worker thread."""
    owners = pool.run(lambda page: (threading.current_thread().name, page.evaluate("1+1")))
    assert owners[0].startswith("browser-worker-") and owners[1] == 2
    assert threading.current_thread().name not in owners


def test_a_failing_job_reports_its_error_and_does_not_poison_the_worker(pool):
    def boom(page):
        raise RuntimeError("job failed")

    with pytest.raises(RuntimeError, match="job failed"):
        pool.run(boom)
    assert pool.run(lambda page: page.evaluate("40+2")) == 42


def test_a_crashed_browser_is_relaunched_for_the_next_job():
    p = BrowserPool(size=1)
    try:
        p.run(lambda page: page.context.browser.close())  # the job takes the whole browser down
        assert p.run(lambda page: page.evaluate("6*7")) == 42
        assert p.stats()["relaunched"] == 1
    finally:
        p.shutdown()


def test_pool_refuses_work_after_shutdown():
    p = BrowserPool(size=1)
    p.shutdown()
    with pytest.raises(PoolClosed):
        p.submit(lambda page: 1)


def test_cookies_do_not_leak_between_consecutive_jobs_on_one_worker(clinic):
    p = BrowserPool(size=1)
    try:
        p.run(lambda page: sign_in(page, clinic))
        cookies_next = p.run(lambda page: (page.goto(f"{clinic}/legacy/menu"), page.url)[1])
        assert cookies_next.endswith("/legacy/login")  # a new context starts signed out, so the menu redirects to sign-on
    finally:
        p.shutdown()
