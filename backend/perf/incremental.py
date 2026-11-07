"""Incremental fetch (§8): ETag/Last-Modified persistence + conditional requests.

- Table `http_cache_meta` (created by migrate_perf) stores per-URL
  {etag, last_modified, updated_at}.
- `conditional_fetch(session, url, ...)` sends If-None-Match / If-Modified-Since
  when we have them; on 304 returns {"not_modified": True} with no body to
  parse — zero wasted parsing, honest about what was skipped.
- For feeds that don't support conditionals (or ignore the headers), use
  `last_timestamp_cursors`: fetch the feed, filter to records newer than the
  stored cursor, and only those go into ingest. Both are honest about what
  they actually skip.

Verified against real endpoints: USGS ComCat GeoJSON supports ETag
(checked at build time — see tests/test_perf_incremental.py).
"""
from __future__ import annotations

import sqlite3
from typing import Any, Callable, Dict, List, Optional

from backend.common.logging_setup import setup_logging

log = setup_logging("aether.perf.incremental")

USGS_ALL_HOUR = ("https://earthquake.usgs.gov/earthquakes/feed/v1.0/"
                 "summary/all_hour.geojson")


def ensure_meta_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS http_cache_meta (
            url TEXT PRIMARY KEY,
            etag TEXT,
            last_modified TEXT,
            updated_at TEXT NOT NULL
        )""")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS perf_ingest_cursors (
            cursor_key TEXT PRIMARY KEY,
            last_seen_value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""")
    conn.commit()


def get_meta(conn: sqlite3.Connection, url: str) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT etag, last_modified FROM http_cache_meta WHERE url=?",
                       (url,)).fetchone()
    return dict(row) if row else None


def save_meta(conn: sqlite3.Connection, url: str, etag: Optional[str],
              last_modified: Optional[str], updated_at: str) -> None:
    conn.execute(
        """INSERT INTO http_cache_meta (url, etag, last_modified, updated_at)
           VALUES (?,?,?,?)
           ON CONFLICT(url) DO UPDATE SET
             etag=excluded.etag, last_modified=excluded.last_modified,
             updated_at=excluded.updated_at""",
        (url, etag, last_modified, updated_at))
    conn.commit()


def conditional_fetch(session, conn: sqlite3.Connection, url: str,
                      timeout: float = 30.0) -> Dict[str, Any]:
    """GET with If-None-Match/If-Modified-Since. Returns dict with
    {ok, not_modified, status_code, data, etag, last_modified, latency_ms}."""
    import time
    meta = get_meta(conn, url) or {}
    headers: Dict[str, str] = {}
    if meta.get("etag"):
        headers["If-None-Match"] = meta["etag"]
    if meta.get("last_modified"):
        headers["If-Modified-Since"] = meta["last_modified"]
    t0 = time.monotonic()
    try:
        resp = session.get(url, headers=headers, timeout=timeout)
        latency_ms = (time.monotonic() - t0) * 1000.0
        if resp.status_code == 304:
            return {"ok": True, "not_modified": True, "status_code": 304,
                    "data": None, "latency_ms": latency_ms, "url": url}
        if resp.status_code != 200:
            return {"ok": False, "not_modified": False,
                    "status_code": resp.status_code, "data": None,
                    "error": f"HTTP {resp.status_code}", "latency_ms": latency_ms,
                    "url": url}
        try:
            data = resp.json()
        except ValueError as e:
            return {"ok": False, "not_modified": False, "status_code": 200,
                    "data": None, "error": f"invalid JSON: {e}",
                    "latency_ms": latency_ms, "url": url}
        etag = resp.headers.get("ETag")
        last_mod = resp.headers.get("Last-Modified")
        if etag or last_mod:
            from backend.common.timeutil import utc_iso
            save_meta(conn, url, etag, last_mod, utc_iso())
        return {"ok": True, "not_modified": False, "status_code": 200,
                "data": data, "etag": etag, "last_modified": last_mod,
                "latency_ms": latency_ms, "url": url}
    except Exception as e:  # noqa: BLE001 - network failure is data
        latency_ms = (time.monotonic() - t0) * 1000.0
        return {"ok": False, "not_modified": False, "status_code": None,
                "data": None, "error": f"{type(e).__name__}: {e}",
                "latency_ms": latency_ms, "url": url}


def get_cursor(conn: sqlite3.Connection, cursor_key: str) -> Optional[str]:
    row = conn.execute("SELECT last_seen_value FROM perf_ingest_cursors WHERE cursor_key=?",
                       (cursor_key,)).fetchone()
    return row["last_seen_value"] if row else None


def set_cursor(conn: sqlite3.Connection, cursor_key: str, value: str,
               updated_at: str) -> None:
    conn.execute(
        """INSERT INTO perf_ingest_cursors (cursor_key, last_seen_value, updated_at)
           VALUES (?,?,?)
           ON CONFLICT(cursor_key) DO UPDATE SET
             last_seen_value=excluded.last_seen_value,
             updated_at=excluded.updated_at""",
        (cursor_key, value, updated_at))
    conn.commit()


def filter_new(conn: sqlite3.Connection, cursor_key: str,
               records: List[Dict[str, Any]],
               key_fn: Callable[[Dict[str, Any]], str]) -> List[Dict[str, Any]]:
    """Keep only records whose key is strictly newer than the stored cursor.
    Updates the cursor to the newest key seen. Keys must be ISO timestamps."""
    cursor = get_cursor(conn, cursor_key)
    new = [r for r in records if not cursor or key_fn(r) > cursor]
    if records:
        newest = max(key_fn(r) for r in records)
        if not cursor or newest > cursor:
            from backend.common.timeutil import utc_iso
            set_cursor(conn, cursor_key, newest, utc_iso())
    return new
