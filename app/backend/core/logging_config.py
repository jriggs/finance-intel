"""
Application logging configuration.

A process-wide ring buffer captures recent WARNING+ records for the
``/api/finance/logs`` viewer, alongside sensible root defaults and third-party
noise suppression. Call :func:`configure_logging` once at startup.
"""
from __future__ import annotations

import collections
import contextlib
import logging

# Ring buffer of recent WARNING+ entries — surfaced via /api/finance/logs.
LOG_BUFFER: collections.deque = collections.deque(maxlen=500)

# Third-party loggers that emit non-actionable noise at INFO/WARNING.
_NOISY_LOGGERS = (
    "transformers", "transformers_modules", "sentence_transformers",
    "chromadb", "httpx", "httpcore", "urllib3", "filelock",
)


class RingBufferHandler(logging.Handler):
    """Append WARNING+ records to :data:`LOG_BUFFER` for the in-app log viewer."""

    def emit(self, record: logging.LogRecord) -> None:
        with contextlib.suppress(Exception):
            LOG_BUFFER.append({
                "ts":      self.formatter.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
                "level":   record.levelname,
                "logger":  record.name,
                "message": record.getMessage(),
            })


class YfCrumbFilter(logging.Filter):
    """Drop yfinance 401 / 'Invalid Crumb' errors — the circuit breaker handles them."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return "Invalid Crumb" not in msg and ("401" not in msg or "yfinance" not in record.name)


def configure_logging() -> None:
    """Install the root config, the ring-buffer handler, and noise filters (idempotent-ish)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(name)-20s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    ring_handler = RingBufferHandler(level=logging.WARNING)
    ring_handler.setFormatter(logging.Formatter())
    logging.getLogger().addHandler(ring_handler)

    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.ERROR)

    logging.getLogger("yfinance").addFilter(YfCrumbFilter())
