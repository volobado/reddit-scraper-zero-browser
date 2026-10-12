"""Async HTTP client for Reddit's public JSON endpoints.

No browser and no OAuth: plain libcurl TLS through ``curl_cffi`` plus the anonymous cookies
that ``old.reddit.com`` hands out on its login page. That combination is what Reddit accepts
for ``www.reddit.com/*.json`` (checked 2026-10-12: without the bootstrap cookies the JSON
endpoints answer 403, and a Chrome TLS impersonation gets 403 on both).

Reddit gives an anonymous session 100 requests per 10-minute window and reports the state
in ``x-ratelimit-*`` headers. :class:`RateLimiter` reads them and makes callers wait for the
window reset instead of hammering the endpoint and collecting 429s.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import random
import re
import time
from dataclasses import asdict, dataclass
from typing import Any

from curl_cffi.requests import AsyncSession
from curl_cffi.requests.exceptions import RequestException

from ._version import __version__

__all__ = [
    "USER_AGENT",
    "ClientStats",
    "RateLimiter",
    "ReddSpectreClient",
    "RedditBadRequest",
    "RedditError",
    "RedditForbidden",
    "RedditNetworkError",
    "RedditNotFound",
    "RedditRateLimited",
    "RedditServerError",
    "RedditSessionError",
]

log = logging.getLogger("reddspectre")

USER_AGENT = f"reddspectre/{__version__} (zero-browser reddit scraper)"
BOOTSTRAP_URL = "https://old.reddit.com/login"
SESSION_TTL = 1800.0  # seconds before the anonymous cookies are refreshed
MAX_RATE_WAIT = 660.0  # never sleep longer than one window plus change
FORBIDDEN_REASONS = {
    "private": "is private",
    "banned": "is banned",
    "quarantined": "is quarantined (needs an opted-in account)",
    "gold_only": "is for Reddit Premium members only",
    "gated": "is gated (needs an opted-in account)",
}
_TARGET = re.compile(r"reddit\.com/(r/[^/?.]+|user/[^/?.]+|comments/[^/?.]+)", re.IGNORECASE)


# ----------------------------------------------------------------------------- errors


class RedditError(Exception):
    """Base class for everything ReddSpectre raises on purpose."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        url: str | None = None,
        reason: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.url = url
        self.reason = reason


class RedditNotFound(RedditError):
    """HTTP 404: the subreddit, user or post does not exist."""


class RedditForbidden(RedditError):
    """HTTP 403 that a fresh session did not fix: private, banned, quarantined, or we are blocked."""


class RedditRateLimited(RedditError):
    """HTTP 429 that persisted through every retry."""


class RedditBadRequest(RedditError):
    """Any other 4xx; retrying would not help."""


class RedditServerError(RedditError):
    """HTTP 5xx (or a non-JSON 200) that persisted through every retry."""


class RedditNetworkError(RedditError):
    """Connection or timeout failure that persisted through every retry."""


RedditSessionError = RedditError  # name used by 0.1.x


def describe_target(url: str) -> str:
    """``r/lounge``, ``user/spez`` or ``post abc123`` for error messages."""
    match = _TARGET.search(url)
    if not match:
        return url
    target = match.group(1)
    return f"post {target.split('/', 1)[1]}" if target.startswith("comments/") else target


def _json_reason(resp: Any) -> str | None:
    try:
        body = resp.json()
    except Exception:
        return None
    if isinstance(body, dict):
        reason = body.get("reason") or body.get("message")
        return str(reason) if reason else None
    return None


def _forbidden_message(url: str, status: int, reason: str | None) -> str:
    target = describe_target(url)
    if reason in FORBIDDEN_REASONS:
        return f"{target} {FORBIDDEN_REASONS[reason]} (HTTP {status}, reason={reason})"
    suffix = f", reason={reason}" if reason else ""
    return f"Reddit refused {target} even with a fresh session (HTTP {status}{suffix})"


# ----------------------------------------------------------------------------- rate limiting


class RateLimiter:
    """Keeps requests inside Reddit's ``x-ratelimit-*`` window.

    ``acquire()`` is awaited before every request. When the last response said the window
    is nearly used up (``remaining - inflight <= reserve``) the caller sleeps until the
    reset the headers announced. ``min_delay`` adds a polite minimum spacing on top.
    """

    def __init__(self, reserve: int = 2, min_delay: float = 0.0) -> None:
        self.reserve = max(0, reserve)
        self.min_delay = max(0.0, min_delay)
        self.remaining: float | None = None
        self.reset_at: float = 0.0
        self.inflight = 0
        self.waits = 0
        self.wait_seconds = 0.0
        self._lock = asyncio.Lock()
        self._last_start = 0.0
        self._announced_reset = 0.0

    def update(self, headers: Any) -> None:
        remaining = headers.get("x-ratelimit-remaining")
        reset = headers.get("x-ratelimit-reset")
        if remaining is None or reset is None:
            return
        try:
            self.remaining = float(remaining)
            self.reset_at = time.monotonic() + float(reset)
        except (TypeError, ValueError):
            pass

    def forget(self) -> None:
        """A new session has its own counter; trust the next response instead."""
        self.remaining = None
        self.reset_at = 0.0

    def window_exhausted(self) -> bool:
        return (
            self.remaining is not None
            and time.monotonic() < self.reset_at
            and (self.remaining - self.inflight) <= self.reserve
        )

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                wait = 0.0
                quota_wait = False
                if self.window_exhausted():
                    wait = max(0.0, self.reset_at - now) + random.uniform(0.5, 1.5)
                    quota_wait = True
                elif self.min_delay:
                    wait = max(0.0, self._last_start + self.min_delay - now)
                if wait <= 0:
                    self.inflight += 1
                    self._last_start = now
                    return
                if quota_wait and self._announced_reset != self.reset_at:
                    # Every concurrent task waits, but it is one pause: count and log it once.
                    self._announced_reset = self.reset_at
                    self.waits += 1
                    self.wait_seconds += wait
                    log.info(
                        "rate limit: %.0f requests left in this window, sleeping %.0fs until it resets",
                        self.remaining or 0,
                        wait,
                    )
            await asyncio.sleep(wait)

    def release(self) -> None:
        self.inflight = max(0, self.inflight - 1)

    def quota(self) -> tuple[float, float] | None:
        """``(requests left, seconds until the window resets)`` from the last response, if any."""
        if self.remaining is None:
            return None
        return self.remaining, max(0.0, self.reset_at - time.monotonic())

    def reset_counters(self) -> None:
        self.waits = 0
        self.wait_seconds = 0.0


@dataclass
class ClientStats:
    requests: int = 0
    retries: int = 0
    rate_limited: int = 0
    sessions: int = 0
    rate_waits: int = 0
    rate_wait_seconds: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ----------------------------------------------------------------------------- client


class ReddSpectreClient:
    """Fetch Reddit JSON with cookie bootstrap, retries, rate limiting and proxy rotation."""

    def __init__(
        self,
        proxies: list[str] | str | None = None,
        concurrency: int = 10,
        max_retries: int = 4,
        impersonate: str | None = None,
        timeout: float = 15.0,
        min_delay: float = 0.0,
        user_agent: str = USER_AGENT,
        rate_reserve: int = 2,
    ) -> None:
        if isinstance(proxies, str):
            proxies = [proxies]
        self.proxies = [p for p in (proxies or []) if p]
        self._proxy_cycle = itertools.cycle(self.proxies) if self.proxies else None
        self.current_proxy: str | None = None
        self.concurrency = max(1, concurrency)
        self.max_retries = max(0, max_retries)
        self.impersonate = impersonate
        self.timeout = timeout
        self.user_agent = user_agent
        self.limiter = RateLimiter(reserve=rate_reserve, min_delay=min_delay)
        self.stats = ClientStats()
        self._semaphore = asyncio.Semaphore(self.concurrency)
        self._lock = asyncio.Lock()
        self._session: AsyncSession | None = None
        self._old_sessions: list[AsyncSession] = []
        self._generation = 0
        self._session_at = 0.0

    # -- sessions ---------------------------------------------------------------------

    @property
    def session(self) -> AsyncSession | None:
        return self._session

    async def _new_session(self) -> AsyncSession:
        self.current_proxy = next(self._proxy_cycle) if self._proxy_cycle else None
        kwargs: dict[str, Any] = {
            "allow_redirects": True,
            "headers": {"User-Agent": self.user_agent},
        }
        if self.current_proxy:
            kwargs["proxy"] = self.current_proxy
        if self.impersonate:
            kwargs["impersonate"] = self.impersonate
        session = AsyncSession(**kwargs)
        try:
            resp = await session.get(BOOTSTRAP_URL, timeout=self.timeout)
        except RequestException as exc:
            await _close_quietly(session)
            raise RedditNetworkError(f"cannot reach Reddit to start a session: {exc}", url=BOOTSTRAP_URL) from exc
        if resp.status_code != 200:
            await _close_quietly(session)
            raise RedditForbidden(
                f"Reddit refused to start a session (HTTP {resp.status_code} on {BOOTSTRAP_URL})"
                + (" while impersonating a browser; try without --impersonate" if self.impersonate else ""),
                status=resp.status_code,
                url=BOOTSTRAP_URL,
            )
        self.limiter.update(resp.headers)
        return session

    async def _reset_session(self, seen_generation: int) -> AsyncSession:
        """Build a fresh session unless another task already did since ``seen_generation``."""
        async with self._lock:
            if self._session is not None and self._generation != seen_generation:
                return self._session
            if self._session is not None:
                # Other tasks may still be mid-request on it; closed on close().
                self._old_sessions.append(self._session)
            self._session = await self._new_session()
            self._generation += 1
            self._session_at = time.monotonic()
            self.limiter.forget()
            self.stats.sessions += 1
            log.debug("session #%d ready (proxy=%s)", self._generation, self.current_proxy or "none")
            return self._session

    async def _get_session(self) -> tuple[AsyncSession, int]:
        if self._session is None or time.monotonic() - self._session_at > SESSION_TTL:
            await self._reset_session(self._generation)
        assert self._session is not None
        return self._session, self._generation

    async def ensure_session(self) -> AsyncSession:
        session, _ = await self._get_session()
        return session

    # -- requests ---------------------------------------------------------------------

    @staticmethod
    def _backoff(attempt: int) -> float:
        return min(2.0**attempt + random.uniform(0.2, 1.0), 20.0)

    @staticmethod
    def _retry_after(resp: Any, attempt: int) -> float:
        for header in ("retry-after", "x-ratelimit-reset"):
            value = resp.headers.get(header)
            if value is None:
                continue
            try:
                return min(max(float(value) + 1.0, 1.0), MAX_RATE_WAIT)
            except (TypeError, ValueError):
                continue
        return min(5.0 * 2**attempt, 120.0) + random.uniform(0.5, 2.0)

    async def get_json(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        *,
        retries: int | None = None,
    ) -> Any:
        """GET ``url`` and return the decoded JSON.

        Retries network errors, 5xx and 429 (waiting for the window reset), refreshes the
        session once on 401/403, and raises a specific :class:`RedditError` otherwise.
        """
        attempts = (self.max_retries if retries is None else max(0, retries)) + 1
        query = dict(params or {})
        query.setdefault("raw_json", 1)  # unescaped text; without it & < > arrive as entities
        refreshed = False
        last_error: RedditError | None = None
        attempt = 0
        while attempt < attempts:
            try:
                session, generation = await self._get_session()
            except RedditNetworkError as exc:  # the bootstrap itself could not reach Reddit
                last_error = exc
                self.stats.retries += 1
                attempt += 1
                if attempt < attempts:
                    log.info("session bootstrap failed (%s), retry %d/%d", exc, attempt, attempts - 1)
                    await asyncio.sleep(self._backoff(attempt))
                continue
            await self.limiter.acquire()
            try:
                async with self._semaphore:
                    self.stats.requests += 1
                    log.debug("GET %s %s", url, {k: v for k, v in query.items() if k != "raw_json"})
                    resp = await session.get(url, params=query, timeout=self.timeout)
            except RequestException as exc:
                self.limiter.release()
                last_error = RedditNetworkError(f"network error for {url}: {exc}", url=url)
                self.stats.retries += 1
                attempt += 1
                if attempt < attempts:
                    log.info("network error for %s (%s), retry %d/%d", url, exc, attempt, attempts - 1)
                    await asyncio.sleep(self._backoff(attempt))
                continue
            self.limiter.release()
            self.limiter.update(resp.headers)
            status = resp.status_code

            if status == 200:
                try:
                    return resp.json()
                except ValueError:
                    last_error = RedditServerError(f"Reddit answered 200 without JSON for {url}", status=200, url=url)
                    self.stats.retries += 1
                    attempt += 1
                    if attempt < attempts:
                        await asyncio.sleep(self._backoff(attempt))
                    continue

            reason = _json_reason(resp)
            if status == 429:
                self.stats.rate_limited += 1
                last_error = RedditRateLimited(f"rate limited (HTTP 429) for {url}", status=429, url=url)
                attempt += 1
                if attempt < attempts:
                    wait = self._retry_after(resp, attempt)
                    log.warning("HTTP 429 for %s, waiting %.0fs for the window to reset", url, wait)
                    await asyncio.sleep(wait)
                continue
            if status in (401, 403):
                if reason in FORBIDDEN_REASONS or refreshed:
                    raise RedditForbidden(
                        _forbidden_message(url, status, reason), status=status, url=url, reason=reason
                    )
                refreshed = True
                log.info("HTTP %s for %s, refreshing the session once", status, url)
                await self._reset_session(generation)
                continue
            if status == 404:
                raise RedditNotFound(
                    f"{describe_target(url)} does not exist (HTTP 404)", status=404, url=url, reason=reason
                )
            if status >= 500:
                self.stats.retries += 1
                last_error = RedditServerError(f"Reddit server error HTTP {status} for {url}", status=status, url=url)
                attempt += 1
                if attempt < attempts:
                    await asyncio.sleep(self._backoff(attempt))
                continue
            suffix = f" ({reason})" if reason else ""
            raise RedditBadRequest(f"HTTP {status} for {url}{suffix}", status=status, url=url, reason=reason)

        assert last_error is not None
        raise last_error

    async def resolve_redirect(self, url: str) -> str | None:
        """Return the ``Location`` of a redirecting URL (share links), else ``None``."""
        session, _ = await self._get_session()
        await self.limiter.acquire()
        try:
            async with self._semaphore:
                self.stats.requests += 1
                resp = await session.get(url, timeout=self.timeout, allow_redirects=False)
        except RequestException as exc:
            raise RedditNetworkError(f"network error for {url}: {exc}", url=url) from exc
        finally:
            self.limiter.release()
        self.limiter.update(resp.headers)
        location = resp.headers.get("location")
        if resp.status_code in (301, 302, 303, 307, 308) and location:
            return location if location.startswith("http") else f"https://www.reddit.com{location}"
        return None

    # -- lifecycle --------------------------------------------------------------------

    def snapshot_stats(self) -> ClientStats:
        self.stats.rate_waits = self.limiter.waits
        self.stats.rate_wait_seconds = round(self.limiter.wait_seconds, 1)
        return self.stats

    def reset_stats(self) -> None:
        self.stats = ClientStats()
        self.limiter.reset_counters()

    async def close(self) -> None:
        async with self._lock:
            sessions = list(self._old_sessions)
            if self._session is not None:
                sessions.append(self._session)
            self._old_sessions.clear()
            self._session = None
        for session in sessions:
            await _close_quietly(session)

    async def __aenter__(self) -> ReddSpectreClient:
        await self.ensure_session()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()


async def _close_quietly(session: AsyncSession) -> None:
    try:
        await session.close()
    except Exception:  # closing is best effort
        pass
