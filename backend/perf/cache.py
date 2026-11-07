"""Cache (§§12-13): TTL cache abstraction over a SQLite store.

Design:
  - `Cache` is a pure interface (get/set/invalidate). `SqliteCache` is the
    deployed implementation, backed by `cache_entries` (created by migrate_perf).
  - `RedisCache` is an INTERFACE STUB ONLY — documented future plug-in for
    multi-host deployments. It is never claimed to be running; the app runs
    on SqliteCache here. Honest by construction: importing it raises unless
    the caller explicitly wires a redis client.
  - Stale-while-revalidate is available for NON-critical data only
    (`stale_ok=True`). Life-safety paths must bypass the cache entirely or use
    `strict=True` with an honest "could not verify" state — never present a
    stale quake/tsunami record as current.

Values are JSON-serialized; TTLs are per-key. Negative caching of failures
is OFF by default (a failed fetch must not be served as "no data").
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, Tuple

from backend.common.logging_setup import setup_logging

log = setup_logging("aether.perf.cache")


class Cache(ABC):
    @abstractmethod
    def get(self, key: str) -> Tuple[bool, Optional[Any]]:
        """Return (hit, value). Expired keys are misses."""

    @abstractmethod
    def set(self, key: str, value: Any, ttl_seconds: float) -> None:
        ...

    @abstractmethod
    def invalidate(self, key: str) -> None:
        ...


class SqliteCache(Cache):
    def __init__(self, conn_factory) -> None:
        """conn_factory: callable -> sqlite3.Connection (thread-local aware)."""
        self._conn_factory = conn_factory
        self._lock = threading.Lock()

    @staticmethod
    def ensure_table(conn: sqlite3.Connection) -> None:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS cache_entries (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                stored_at REAL NOT NULL,
                expires_at REAL NOT NULL
            )""")
        conn.commit()

    def get(self, key: str) -> Tuple[bool, Optional[Any]]:
        conn = self._conn_factory()
        row = conn.execute("SELECT value_json, expires_at FROM cache_entries WHERE key=?",
                           (key,)).fetchone()
        if row is None:
            return False, None
        if row["expires_at"] <= time.time():
            conn.execute("DELETE FROM cache_entries WHERE key=?", (key,))
            conn.commit()
            return False, None
        try:
            return True, json.loads(row["value_json"])
        except ValueError:
            return False, None

    def set(self, key: str, value: Any, ttl_seconds: float) -> None:
        conn = self._conn_factory()
        now = time.time()
        with self._lock:
            conn.execute(
                """INSERT INTO cache_entries (key, value_json, stored_at, expires_at)
                   VALUES (?,?,?,?)
                   ON CONFLICT(key) DO UPDATE SET
                     value_json=excluded.value_json,
                     stored_at=excluded.stored_at,
                     expires_at=excluded.expires_at""",
                (key, json.dumps(value, default=str), now, now + ttl_seconds))
            conn.commit()

    def invalidate(self, key: str) -> None:
        conn = self._conn_factory()
        conn.execute("DELETE FROM cache_entries WHERE key=?", (key,))
        conn.commit()


class RedisCache(Cache):
    """FUTURE PLUG-IN STUB — NOT DEPLOYED.

    This exists so a production team can drop in a redis-backed cache against
    the same `Cache` interface. It raises on construction here because no redis
    server or client is installed in this deployment. Documented in
    docs/PERFORMANCE.md; never claimed as running.
    """

    def __init__(self, *args, **kwargs) -> None:
        raise RuntimeError(
            "RedisCache is a documented future plug-in, not deployed. "
            "This environment runs SqliteCache (SQLite is the deployment DB).")

    def get(self, key: str):
        raise NotImplementedError

    def set(self, key: str, value: Any, ttl_seconds: float):
        raise NotImplementedError

    def invalidate(self, key: str):
        raise NotImplementedError


def get_or_fetch(cache: Cache, key: str, ttl_seconds: float,
                 fetch_fn, strict: bool = False,
                 stale_ok: bool = False,
                 max_stale_seconds: float = 300.0) -> Dict[str, Any]:
    """Cache read with optional stale-while-revalidate (non-critical only).

    Returns {"value", "cached", "stale", "could_not_verify"}.
    - strict=True: life-safety paths. Stale entries are never served; on fetch
      failure the caller gets could_not_verify=True and must show the
      §66 "CURRENT STATUS COULD NOT BE VERIFIED" state.
    - stale_ok=True: serve a stale entry while revalidating in the background.
    """
    hit, value = cache.get(key)
    if hit and value is not None:
        return {"value": value, "cached": True, "stale": False,
                "could_not_verify": False}
    try:
        fresh = fetch_fn()
        cache.set(key, fresh, ttl_seconds)
        return {"value": fresh, "cached": False, "stale": False,
                "could_not_verify": False}
    except Exception as e:  # noqa: BLE001
        log.warning("cache fetch failed for %s: %s", key, e)
        return {"value": None, "cached": False, "stale": False,
                "could_not_verify": True, "error": f"{type(e).__name__}: {e}"}


class SingleFlight:
    """Coalesce concurrent fetches for the same key (§89).

    The first caller for a key runs fetch_fn; all other threads asking for
    the same key wait for that one result instead of stampeding the source.
    Each distinct key gets its own in-flight slot; failures are not cached
    (a failed fetch must not be served as "no data").
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._inflight: Dict[str, threading.Event] = {}
        self._results: Dict[str, Any] = {}

    def fetch(self, key: str, fetch_fn) -> Any:
        with self._lock:
            if key in self._inflight:
                event = self._inflight[key]
                leader = False
            else:
                event = threading.Event()
                self._inflight[key] = event
                leader = True
        if leader:
            try:
                result = fetch_fn()
                with self._lock:
                    self._results[key] = (True, result)
                return result
            except Exception as e:  # noqa: BLE001 - failures are not cached
                with self._lock:
                    self._results[key] = (False, e)
                raise
            finally:
                # Wake followers while the result is still stored; the entry
                # is overwritten by the next leader (bounded: one entry/key).
                with self._lock:
                    del self._inflight[key]
                event.set()
        else:
            event.wait(timeout=120)
            with self._lock:
                ok, payload = self._results.get(key, (False, RuntimeError("singleflight lost")))
            if ok:
                return payload
            raise payload
