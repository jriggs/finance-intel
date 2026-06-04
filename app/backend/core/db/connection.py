"""
SQLite connection management: a thread-local connection pool and the shared
write lock. One connection per thread (WAL mode means readers never block
writers); every write serializes through ``_write_lock``.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
from pathlib import Path

logger = logging.getLogger("portfolio")

# The DB lives at app/data/finance.db by default; override with FINANCE_DB_PATH.
# This file is app/backend/core/db/connection.py → four parents reach app/.
_DEFAULT_DB = Path(__file__).parent.parent.parent.parent / "data" / "finance.db"
DB_PATH = Path(os.environ.get("FINANCE_DB_PATH", str(_DEFAULT_DB)))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

_local = threading.local()
_write_lock = threading.Lock()


def _get_conn() -> sqlite3.Connection:
    """Return this thread's sqlite3 connection, creating it on first use."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")       # readers never block writers
        conn.execute("PRAGMA synchronous=NORMAL")     # safe with WAL, no per-write fsync
        conn.execute("PRAGMA cache_size=-32000")      # 32 MB page cache per connection
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA mmap_size=268435456")    # 256 MB memory-mapped I/O
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return conn
