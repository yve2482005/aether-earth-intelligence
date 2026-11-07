"""Connection handling (§32): thread-local SQLite connections.

Single-host deployment model, stated honestly:
  - One SQLite file (WAL mode), one host, many threads.
  - Each thread gets its own connection (sqlite3 connections are NOT shared
    across threads without check_same_thread=False; we give each thread its
    own instead of fighting the lock).
  - busy_timeout=30s so brief writer contention waits instead of erroring.
  - This does NOT scale past one host; the multi-host path is a client/server
    DB (documented in docs/PERFORMANCE.md as the production plug-in, not
    deployed here).
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Optional

from backend.db import db as dbmod

_thread_local = threading.local()


def conn_for(path: Optional[str | Path] = None) -> sqlite3.Connection:
    """Return the calling thread's connection to `path` (created on demand)."""
    key = str(path or "default")
    store = getattr(_thread_local, "conns", None)
    if store is None:
        store = {}
        _thread_local.conns = store
    conn = store.get(key)
    if conn is None:
        conn = dbmod.connect(path)
        store[key] = conn
    return conn


def conn_factory(path: Optional[str | Path] = None):
    """Return a zero-arg factory usable by SqliteCache / workers."""
    def _factory() -> sqlite3.Connection:
        return conn_for(path)
    return _factory


def close_all() -> None:
    store = getattr(_thread_local, "conns", None)
    if store:
        for c in store.values():
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
        store.clear()
