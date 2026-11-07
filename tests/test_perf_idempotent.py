"""Perf tests: idempotent ingestion (§10) — double-ingest creates zero duplicates."""
from __future__ import annotations

import sqlite3

from backend.perf import ids
from backend.perf.ids import (alert_identity, already_ingested, content_hash,
                               insert_once, quake_identity)


def _mem():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE ev (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  event_id TEXT, mag REAL, h TEXT UNIQUE)""")
    return c


def test_content_hash_canonical_across_key_order():
    a = content_hash({"x": 1, "y": [2, 3], "z": "s"})
    b = content_hash({"z": "s", "y": [2, 3], "x": 1})
    assert a == b and len(a) == 64


def test_quake_identity_stable_and_distinct():
    h1 = quake_identity("usgs", "us123", "2026-10-05T00:00:00Z", 5.8, 16.8, 96.1)
    h2 = quake_identity("usgs", "us123", "2026-10-05T00:00:00Z", 5.8, 16.8, 96.1)
    h3 = quake_identity("usgs", "us124", "2026-10-05T00:00:00Z", 5.8, 16.8, 96.1)
    assert h1 == h2 and h1 != h3


def test_double_ingest_creates_zero_duplicates():
    conn = _mem()
    h = quake_identity("usgs", "us999", "2026-10-05T00:00:00Z", 6.0, 16.8, 96.1)
    first = insert_once(conn, "ev", "h", h, ["event_id", "mag"], ["us999", 6.0])
    second = insert_once(conn, "ev", "h", h, ["event_id", "mag"], ["us999", 6.0])
    assert first is True and second is False
    n = conn.execute("SELECT COUNT(*) c FROM ev").fetchone()["c"]
    assert n == 1
    assert already_ingested(conn, "ev", "h", h)


def test_burst_ingest_of_same_event_is_idempotent():
    conn = _mem()
    inserted = 0
    for _ in range(50):  # 50 workers seeing the same event
        h = quake_identity("usgs", "usBurst", "2026-10-05T00:00:00Z", 7.1, 16.8, 96.1)
        if insert_once(conn, "ev", "h", h, ["event_id", "mag"], ["usBurst", 7.1]):
            inserted += 1
    assert inserted == 1
    assert conn.execute("SELECT COUNT(*) c FROM ev").fetchone()["c"] == 1


def test_alert_identity():
    a1 = alert_identity("EARTHQUAKE", 1, "M6.0 quake", "2026-10-05T00:00:00Z")
    a2 = alert_identity("EARTHQUAKE", 1, "M6.0 quake", "2026-10-05T00:00:00Z")
    assert a1 == a2
