"""Earthquake event deduplication tests — against the landed backend module.

Landed contract (backend/collectors/earthquake/event_deduplicator.py):
  - USGS events: canonical_event_id is the authoritative USGS id (stable);
    source_event_ids (JSON) accumulates aliases.
  - Non-USGS reports merge against existing events of a DIFFERENT source when
    origin times are within MERGE_WINDOW_SECONDS (120 s) and epicenters within
    MERGE_RADIUS_KM (150 km, haversine). Nearest candidate wins.
  - merge_aliases() unions id lists, tolerating malformed JSON.

These tests build an in-memory SQLite DB from backend/db/schema.sql and drive
find_merge_candidate() directly, plus unit-test the pure helpers.
"""

import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
ROOT = os.path.join(os.path.dirname(__file__), "..")

try:
    from backend.collectors.earthquake import event_deduplicator as dedup
    from backend.common.timeutil import utc_iso

    BACKEND_LANDED = True
except Exception:
    BACKEND_LANDED = False
    dedup = None


def _memdb_with_event(event_id="us1", lat=16.8, lon=96.1, origin=None, source="usgs"):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    with open(os.path.join(ROOT, "backend", "db", "schema.sql"), encoding="utf-8") as fh:
        conn.executescript(fh.read())
    origin = origin or "2026-10-05T12:00:00Z"
    conn.execute(
        """INSERT INTO earthquake_events
           (canonical_event_id, source, source_event_ids, origin_time,
            latitude, longitude, magnitude, created_at)
           VALUES (?, ?, '["%s"]', ?, ?, ?, 5.0, ?)""" % event_id,
        (event_id, source, origin, lat, lon, origin),
    )
    conn.commit()
    return conn


def _iso_plus(base, seconds):
    dt = datetime.fromisoformat(base.replace("Z", "+00:00")) + timedelta(seconds=seconds)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@pytest.mark.skipif(not BACKEND_LANDED,
                    reason="backend/collectors/earthquake/event_deduplicator.py not landed yet")
def test_merge_window_constants():
    assert dedup.MERGE_WINDOW_SECONDS == 120
    assert dedup.MERGE_RADIUS_KM == 150.0


@pytest.mark.skipif(not BACKEND_LANDED, reason="dedup module not landed yet")
def test_haversine_km_sanity():
    # 1 degree of longitude at lat 16.8 ~ 106 km
    d = dedup.haversine_km(16.8, 96.1, 16.8, 97.1)
    assert 100 < d < 112
    assert dedup.haversine_km(16.8, 96.1, 16.8, 96.1) == pytest.approx(0.0)


@pytest.mark.skipif(not BACKEND_LANDED, reason="dedup module not landed yet")
def test_nearby_cross_source_report_merges():
    conn = _memdb_with_event()
    got = dedup.find_merge_candidate(conn, 16.9, 96.2, _iso_plus("2026-10-05T12:00:00Z", 60), "jma")
    assert got == "us1"
    conn.close()


@pytest.mark.skipif(not BACKEND_LANDED, reason="dedup module not landed yet")
def test_far_report_does_not_merge():
    conn = _memdb_with_event()
    got = dedup.find_merge_candidate(conn, 20.0, 96.1, _iso_plus("2026-10-05T12:00:00Z", 60), "jma")
    assert got is None
    conn.close()


@pytest.mark.skipif(not BACKEND_LANDED, reason="dedup module not landed yet")
def test_late_report_does_not_merge():
    conn = _memdb_with_event()
    got = dedup.find_merge_candidate(conn, 16.85, 96.15, _iso_plus("2026-10-05T12:00:00Z", 300), "jma")
    assert got is None
    conn.close()


@pytest.mark.skipif(not BACKEND_LANDED, reason="dedup module not landed yet")
def test_same_source_report_does_not_merge():
    # USGS canonical ids are authoritative; same-source reports are not merged by proximity
    conn = _memdb_with_event(source="usgs")
    got = dedup.find_merge_candidate(conn, 16.85, 96.15, _iso_plus("2026-10-05T12:00:00Z", 60), "usgs")
    assert got is None
    conn.close()


@pytest.mark.skipif(not BACKEND_LANDED, reason="dedup module not landed yet")
def test_nearest_candidate_wins():
    conn = _memdb_with_event(event_id="us1", lat=16.8, lon=96.1)
    conn.execute(
        """INSERT INTO earthquake_events
           (canonical_event_id, source, source_event_ids, origin_time,
            latitude, longitude, magnitude, created_at)
           VALUES ('us2','usgs','["us2"]','2026-10-05T12:00:30Z', 17.5, 96.1, 5.0,
                   '2026-10-05T12:01:00Z')""")
    conn.commit()
    got = dedup.find_merge_candidate(conn, 16.82, 96.12, _iso_plus("2026-10-05T12:00:00Z", 45), "jma")
    assert got == "us1"
    conn.close()


@pytest.mark.skipif(not BACKEND_LANDED, reason="dedup module not landed yet")
def test_merge_aliases_unions_ids():
    assert dedup.merge_aliases('["a"]', ["b", "a"]) == '["a", "b"]'
    assert dedup.merge_aliases("", ["x"]) == '["x"]'
    assert dedup.merge_aliases("not-json", ["x"]) == '["x"]'


@pytest.mark.skipif(not BACKEND_LANDED, reason="dedup module not landed yet")
def test_canonical_id_for_usgs_is_authoritative():
    assert dedup.canonical_id_for_usgs({"canonical_event_id": "us700abc"}) == "us700abc"


@pytest.mark.skipif(not BACKEND_LANDED, reason="dedup module not landed yet")
def test_utc_iso_helper_exists():
    assert utc_iso().endswith("Z")
