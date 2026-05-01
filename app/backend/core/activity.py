"""
Server activity pub/sub — broadcast human-friendly events to all connected SSE clients.

Each /api/activity connection gets its own asyncio.Queue.
emit() is fully non-blocking (put_nowait with a per-queue size cap so a slow
client never stalls the server).
"""

import asyncio
import time
from dataclasses import dataclass, field

__all__ = ["ActivityEvent", "emit", "subscribe", "unsubscribe"]

# ── Event model ────────────────────────────────────────────────────────────────

@dataclass(slots=True)
class ActivityEvent:
    icon:   str
    label:  str
    detail: str   = ""
    ts:     float = field(default_factory=time.time)


# ── Subscriber registry ────────────────────────────────────────────────────────
# A plain set is fine — all mutations happen on the event loop thread.

_subscribers: set[asyncio.Queue] = set()

_QUEUE_MAX = 60   # events buffered per subscriber before older ones are dropped


# ── Public API ─────────────────────────────────────────────────────────────────

def emit(icon: str, label: str, detail: str = "") -> None:
    """
    Broadcast an activity event to every connected SSE client.
    Safe to call from any coroutine running on the main event loop.
    Drops events for slow / full queues rather than blocking.
    """
    event = ActivityEvent(icon=icon, label=label, detail=detail)
    dead: list[asyncio.Queue] = []
    for q in _subscribers:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            dead.append(q)   # evict unresponsive subscribers
    for q in dead:
        _subscribers.discard(q)


def subscribe() -> asyncio.Queue:
    """Register a new SSE client and return its event queue."""
    q: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAX)
    _subscribers.add(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    """Remove a client queue when its SSE connection closes."""
    _subscribers.discard(q)
