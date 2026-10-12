import time

import pytest

from reddspectre import client as client_module
from reddspectre.client import RateLimiter


@pytest.fixture(autouse=True)
def no_jitter(monkeypatch):
    monkeypatch.setattr(client_module.random, "uniform", lambda a, b: 0.0)


async def test_waits_until_window_reset_when_quota_is_nearly_used():
    limiter = RateLimiter(reserve=1)
    limiter.update({"x-ratelimit-remaining": "1", "x-ratelimit-reset": "0.4"})
    assert limiter.window_exhausted()
    started = time.monotonic()
    await limiter.acquire()
    assert time.monotonic() - started >= 0.35
    assert limiter.waits == 1 and limiter.wait_seconds >= 0.35
    assert limiter.inflight == 1
    limiter.release()
    assert limiter.inflight == 0


async def test_concurrent_waiters_count_as_one_pause():
    import asyncio

    limiter = RateLimiter(reserve=0)
    limiter.update({"x-ratelimit-remaining": "0", "x-ratelimit-reset": "0.3"})
    await asyncio.gather(limiter.acquire(), limiter.acquire(), limiter.acquire())
    assert limiter.inflight == 3
    assert limiter.waits == 1, "ten tasks sleeping through the same window is one wait, not ten"
    assert 0.25 <= limiter.wait_seconds < 1.0


async def test_no_wait_when_quota_is_plentiful():
    limiter = RateLimiter(reserve=2)
    limiter.update({"x-ratelimit-remaining": "50", "x-ratelimit-reset": "300"})
    started = time.monotonic()
    await limiter.acquire()
    assert time.monotonic() - started < 0.05 and limiter.waits == 0


async def test_inflight_requests_count_against_the_window():
    limiter = RateLimiter(reserve=0)
    limiter.update({"x-ratelimit-remaining": "3", "x-ratelimit-reset": "300"})
    await limiter.acquire()
    await limiter.acquire()
    await limiter.acquire()
    assert limiter.window_exhausted(), "three in flight and three remaining: the next one must wait"
    limiter.release()
    assert not limiter.window_exhausted()


async def test_min_delay_spaces_requests():
    limiter = RateLimiter(min_delay=0.2)
    await limiter.acquire()
    limiter.release()
    started = time.monotonic()
    await limiter.acquire()
    assert time.monotonic() - started >= 0.15
    assert limiter.waits == 0, "polite delay is not a quota wait"


def test_quota_and_counter_reset():
    limiter = RateLimiter()
    assert limiter.quota() is None
    limiter.update({"x-ratelimit-remaining": "42", "x-ratelimit-reset": "120"})
    left, seconds = limiter.quota()
    assert left == 42.0 and 118 < seconds <= 120
    limiter.waits, limiter.wait_seconds = 3, 9.5
    limiter.reset_counters()
    assert limiter.waits == 0 and limiter.wait_seconds == 0.0


def test_forget_and_garbage_headers():
    limiter = RateLimiter()
    limiter.update({"x-ratelimit-remaining": "0", "x-ratelimit-reset": "100"})
    assert limiter.window_exhausted()
    limiter.forget()
    assert not limiter.window_exhausted() and limiter.remaining is None
    limiter.update({"x-ratelimit-remaining": "abc", "x-ratelimit-reset": "100"})
    assert limiter.remaining is None
    limiter.update({})
    assert limiter.remaining is None
