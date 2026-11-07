"""Perf tests: cache abstraction (§§12-13). Hermetic."""
from __future__ import annotations

import sqlite3
import time

import pytest

from backend.perf.cache import RedisCache, SqliteCache, get_or_fetch


def _mem_conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    return c


def test_sqlite_cache_set_get():
    c = _mem_conn()
    SqliteCache.ensure_table(c)
    cache = SqliteCache(lambda: c)
    cache.set("k1", {"a": 1}, ttl_seconds=60)
    hit, val = cache.get("k1")
    assert hit and val == {"a": 1}


def test_sqlite_cache_ttl_expiry():
    c = _mem_conn()
    SqliteCache.ensure_table(c)
    cache = SqliteCache(lambda: c)
    cache.set("k2", "v", ttl_seconds=0.05)
    time.sleep(0.08)
    hit, _ = cache.get("k2")
    assert not hit


def test_sqlite_cache_invalidate():
    c = _mem_conn()
    SqliteCache.ensure_table(c)
    cache = SqliteCache(lambda: c)
    cache.set("k3", "v", ttl_seconds=60)
    cache.invalidate("k3")
    hit, _ = cache.get("k3")
    assert not hit


def test_redis_cache_is_stub_only():
    with pytest.raises(RuntimeError, match="not deployed"):
        RedisCache()


def test_get_or_fetch_strict_never_serves_stale():
    c = _mem_conn()
    SqliteCache.ensure_table(c)
    cache = SqliteCache(lambda: c)

    def _boom():
        raise ConnectionError("down")

    res = get_or_fetch(cache, "life", 60, _boom, strict=True)
    assert res["could_not_verify"] is True
    assert res["value"] is None


def test_get_or_fetch_caches_fresh():
    c = _mem_conn()
    SqliteCache.ensure_table(c)
    cache = SqliteCache(lambda: c)
    calls = []

    def _fetch():
        calls.append(1)
        return {"fresh": True}

    r1 = get_or_fetch(cache, "x", 60, _fetch)
    r2 = get_or_fetch(cache, "x", 60, _fetch)
    assert r1["cached"] is False and r2["cached"] is True
    assert len(calls) == 1  # second read served from cache


def test_cache_factory_contract():
    # any zero-arg factory returning sqlite3.Row connections is accepted;
    # thread-local factories keep one connection per thread (§32)
    c = _mem_conn()
    SqliteCache.ensure_table(c)
    cache = SqliteCache(lambda: c)
    cache.set("t", 1, ttl_seconds=60)
    hit, val = cache.get("t")
    assert hit and val == 1
