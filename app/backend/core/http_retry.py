"""
Reusable retry/backoff utilities for external HTTP service calls.

Usage — sync (fundamentals, signals):
    result = retry_sync(lambda: client.get(url).raise_for_status(), SEC_RETRY)

Usage — async (scraper, crawler):
    resp = await retry_async(lambda: client.get(url), WEB_RETRY)
    resp.raise_for_status()
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger("http_retry")

# Server / rate-limit errors worth retrying
_RETRYABLE_STATUS: frozenset[int] = frozenset({429, 500, 502, 503, 504})

# Client errors — retrying won't help
_NO_RETRY_STATUS: frozenset[int] = frozenset({400, 401, 403, 404, 422})

# Network-level exceptions that are transient
_RETRYABLE_EXC = (
    httpx.TimeoutException,
    httpx.ConnectError,
    httpx.RemoteProtocolError,
    httpx.ReadError,
)


@dataclass
class RetryConfig:
    """Controls retry behaviour for a category of external calls."""
    max_retries: int = 3
    base_delay: float = 0.5      # seconds; doubles each attempt
    max_delay: float = 30.0      # backoff cap
    retryable_status: frozenset[int] = field(default_factory=lambda: _RETRYABLE_STATUS)
    no_retry_status: frozenset[int] = field(default_factory=lambda: _NO_RETRY_STATUS)


# Pre-built configs for common scenarios
DEFAULT_RETRY   = RetryConfig()                                  # general external APIs
SEC_RETRY       = RetryConfig(max_retries=4, base_delay=1.0)    # SEC EDGAR (strict rate limits)
WEB_RETRY       = RetryConfig(max_retries=2, base_delay=0.3)    # web scraping (fail fast)
LIGHT_RETRY     = RetryConfig(max_retries=2, base_delay=0.25)   # low-latency paths


def exponential_backoff(attempt: int, base_delay: float = 0.5, max_delay: float = 30.0) -> float:
    """Return jittered exponential backoff delay in seconds. Safe to use outside RetryConfig."""
    raw = base_delay * (2 ** attempt) + random.uniform(0, 0.5)
    return min(raw, max_delay)


def _backoff(attempt: int, config: RetryConfig) -> float:
    return exponential_backoff(attempt, config.base_delay, config.max_delay)


def _retryable_status(code: int, config: RetryConfig) -> bool:
    return code not in config.no_retry_status and code in config.retryable_status


def retry_sync(fn, config: RetryConfig = DEFAULT_RETRY):
    """
    Call fn() with exponential-backoff retry on transient HTTP errors.

    fn must be a zero-arg callable that either returns a value or raises.
    Re-raises immediately on non-retryable status codes (4xx) or after
    exhausting retries.

        def _fetch():
            with httpx.Client(timeout=15) as c:
                r = c.get(url)
                r.raise_for_status()
                return r.json()
        data = retry_sync(_fetch, SEC_RETRY)
    """
    for attempt in range(config.max_retries + 1):
        try:
            return fn()
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if not _retryable_status(code, config) or attempt >= config.max_retries:
                raise
            delay = _backoff(attempt, config)
            logger.debug("HTTP %d — retry %d/%d in %.1fs", code, attempt + 1, config.max_retries, delay)
            time.sleep(delay)
        except _RETRYABLE_EXC as exc:
            if attempt >= config.max_retries:
                raise
            delay = _backoff(attempt, config)
            logger.debug("%s — retry %d/%d in %.1fs", type(exc).__name__, attempt + 1, config.max_retries, delay)
            time.sleep(delay)


async def retry_async(fn, config: RetryConfig = DEFAULT_RETRY):
    """
    Await fn() with exponential-backoff retry on transient HTTP errors.

    fn must be a zero-arg async callable (lambda or coroutine factory).
    Returns the response object; caller is responsible for raise_for_status()
    on non-retryable codes.

        resp = await retry_async(lambda: client.get(url), WEB_RETRY)
        if resp.status_code == 200:
            ...
    """
    for attempt in range(config.max_retries + 1):
        try:
            return await fn()
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if not _retryable_status(code, config) or attempt >= config.max_retries:
                raise
            delay = _backoff(attempt, config)
            logger.debug("HTTP %d — retry %d/%d in %.1fs", code, attempt + 1, config.max_retries, delay)
            await asyncio.sleep(delay)
        except _RETRYABLE_EXC as exc:
            if attempt >= config.max_retries:
                raise
            delay = _backoff(attempt, config)
            logger.debug("%s — retry %d/%d in %.1fs", type(exc).__name__, attempt + 1, config.max_retries, delay)
            await asyncio.sleep(delay)
