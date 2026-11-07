"""GDACS adapter tests — fixture only, no network.

The fixture mirrors the live GDACS API shape verified 2026-11-06:
GeoJSON FeatureCollection, properties eventtype/eventid/episodeid/
eventname/glide/name, geometry Point [lon, lat].
"""
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.collectors.disaster import gdacs  # noqa: E402

FIXTURE = {
    "type": "FeatureCollection",
    "features": [
        {"type": "Feature",
         "bbox": [12.876, 48.127, 12.876, 48.127],
         "geometry": {"type": "Point", "coordinates": [12.876, 48.127]},
         "properties": {"eventtype": "DR", "eventid": 1018332,
                        "episodeid": 26, "eventname": "Europe-2026",
                        "glide": "DR-2025-000239-ALB",
                        "name": "Drought in Andorra, Albania",
                        "severity": "Moderate", "severityvalue": 1.5,
                        "fromdate": "2026-10-01T00:00:00",
                        "todate": "2026-11-06T00:00:00",
                        "url": "https://www.gdacs.org/report.aspx?eventtype=DR&eventid=1018332"}},
        {"type": "Feature",
         "geometry": {"type": "Point", "coordinates": [96.17, 16.84]},
         "properties": {"eventtype": "TC", "eventid": 1001234,
                        "episodeid": 1, "eventname": "Test-Cyclone",
                        "glide": "TC-2026-000001-MMR",
                        "name": "Tropical Cyclone Test",
                        "severity": "High", "severityvalue": 3.0,
                        "fromdate": "2026-11-05T00:00:00",
                        "todate": "2026-11-06T00:00:00",
                        "url": "https://www.gdacs.org/report.aspx?eventtype=TC&eventid=1001234"}},
        {"type": "Feature",  # malformed: no event id -> dropped
         "geometry": {"type": "Point", "coordinates": [0.0, 0.0]},
         "properties": {"eventtype": "FL"}},
        {"type": "Feature",  # impossible latitude -> dropped
         "geometry": {"type": "Point", "coordinates": [0.0, 91.0]},
         "properties": {"eventtype": "FL", "eventid": 999}},
    ],
}


def test_parse_and_store():
    events = [gdacs._parse_feature(f) for f in FIXTURE["features"]]
    events = [e for e in events if e is not None]
    assert len(events) == 2, events
    assert events[0]["eventtype"] == "DR" and events[0]["eventid"] == 1018332
    assert events[0]["latitude"] == 48.127 and events[0]["longitude"] == 12.876
    assert events[1]["glide"] == "TC-2026-000001-MMR"

    conn = sqlite3.connect(":memory:")
    stored = gdacs.store_events(conn, events)
    assert stored == {"inserted": 2, "seen": 2, "skipped_duplicate": 0}, stored
    # Second store is a pure dedup: 0 inserted.
    stored2 = gdacs.store_events(conn, events)
    assert stored2["inserted"] == 0 and stored2["skipped_duplicate"] == 2, stored2
    rows = conn.execute("SELECT eventtype, eventid FROM gdacs_events ORDER BY eventid").fetchall()
    assert rows == [("TC", 1001234), ("DR", 1018332)], rows


def test_collect_uses_session_fixture():
    class FakeSession:
        def get(self, url, params=None, timeout=None):
            raise AssertionError("adapter must use fetch_json, not raw get")

    # fetch_json is the project's helper; stub it at the module level.
    calls = {"n": 0}

    def fake_fetch_json(session, url, params=None, timeout=None):
        calls["n"] += 1
        return {"ok": True, "status_code": 200, "latency_ms": 12,
                "data": FIXTURE, "error": None, "url": url, "attempts": 1}

    orig = gdacs.fetch_json
    gdacs.fetch_json = fake_fetch_json
    try:
        conn = sqlite3.connect(":memory:")
        result = gdacs.collect(conn, session=FakeSession(), days=7)
    finally:
        gdacs.fetch_json = orig
    # One call returns the whole fixture (2 distinct events).
    assert result["status"] == "success", result
    assert result["fetched"] == 2 and result["inserted"] == 2, result
    assert result["skipped_duplicate"] == 0, result
    assert calls["n"] == 1, calls
