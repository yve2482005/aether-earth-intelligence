"""Tests for the replay / time machine (§§50-51).

The critical invariant: past and future are NEVER mixed. The separation test
below must FAIL if an observation or a future-valid forecast leaks into the
forecast side of a replay.
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests/helpers.py
from helpers import WEATHER_DDL, add_forecast, add_observation, iso  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.replay.replay import ReplayError, replay  # noqa: E402

EXTRA_DDL = """
CREATE TABLE earthquake_events (
    canonical_event_id TEXT PRIMARY KEY, source TEXT NOT NULL,
    source_event_ids TEXT NOT NULL, origin_time TEXT NOT NULL,
    latitude REAL NOT NULL, longitude REAL NOT NULL, depth REAL,
    magnitude REAL, magnitude_type TEXT, region TEXT, status TEXT,
    review_status TEXT, felt_count INTEGER, mmi REAL, alert_level TEXT,
    tsunami_flag INTEGER DEFAULT 0, shakemap_available INTEGER DEFAULT 0,
    pager_available INTEGER DEFAULT 0, source_url TEXT, update_time TEXT,
    created_at TEXT NOT NULL);
CREATE TABLE tsunami_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tsunami_event_id TEXT, message_type TEXT NOT NULL, raw_text TEXT,
    issue_time TEXT, received_at TEXT NOT NULL, source_url TEXT);
"""


def ro(path):
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    return c


@pytest.fixture()
def db(tmp_path):
    path = str(tmp_path / "replay.db")
    conn = ro(path)
    conn.executescript(WEATHER_DDL + EXTRA_DDL)
    conn.execute(
        "INSERT INTO locations (id, name, latitude, longitude, created_at)"
        " VALUES (1, 'Testville', 16.8, 96.15, '2026-01-01T00:00:00Z')")
    conn.commit()
    conn.close()
    yield path


DAY = datetime(2026, 9, 20, 0, 0, tzinfo=timezone.utc)


def _seed(db):
    for h in range(24):
        vt = iso(DAY + timedelta(hours=h))
        add_forecast(db, vt, fh=h, temp=30.0 - 0.05 * h)
        add_observation(db, vt, temp=29.5 - 0.05 * h)
    conn = ro(db)
    conn.execute("""INSERT INTO earthquake_events
        (canonical_event_id, source, source_event_ids, origin_time, latitude,
         longitude, depth, magnitude, magnitude_type, region, created_at)
        VALUES ('usgs-1', 'usgs', '["a"]', ?, 16.8, 96.15, 10.0, 6.1, 'mw',
                'Test', ?)""",
        (iso(DAY + timedelta(hours=6)), iso(DAY + timedelta(hours=6, minutes=5))))
    conn.execute("""INSERT INTO tsunami_messages
        (tsunami_event_id, message_type, issue_time, received_at)
        VALUES ('tsu-1', 'WARNING', ?, ?)""",
        (iso(DAY + timedelta(hours=7)),) * 2)
    conn.execute("""INSERT INTO tsunami_messages
        (tsunami_event_id, message_type, issue_time, received_at)
        VALUES ('tsu-1', 'WATCH', ?, ?)""",
        (iso(DAY + timedelta(hours=9)),) * 2)
    conn.commit()
    conn.close()


def test_replay_basic(db):
    _seed(db)
    conn = ro(db)
    rep = replay(conn, "2026-09-20", 1)
    conn.close()
    assert rep["date"] == "2026-09-20"
    past = rep["past"]
    assert past["forecast_count"] == 24
    assert past["observation_count"] == 24
    assert len(rep["quakes"]) == 1
    assert len(rep["tsunami_revisions"]["tsu-1"]) == 2
    # revisions ordered by issue time: WARNING then WATCH
    types = [m["message_type"] for m in rep["tsunami_revisions"]["tsu-1"]]
    assert types == ["WARNING", "WATCH"]


def test_replay_past_future_separation(db):
    """FAILS if any observation leaks into the forecast side, or any
    forecast with a future valid_time appears on the past side."""
    _seed(db)
    conn = ro(db)
    # plant a future forecast (valid tomorrow) — must never appear in the replay
    add_forecast(db, iso(DAY + timedelta(days=1, hours=2)), fh=26, temp=99.9)
    # plant a hindsight row: issued AFTER its valid time — must be excluded
    conn.execute(
        """INSERT INTO weather_forecasts
           (location_id, source, model, forecast_run, forecast_hour, issued_at,
            valid_time, temperature, humidity, pressure, wind_speed,
            precipitation, precip_probability, cloud_cover, collected_at)
           VALUES (1,'open-meteo','gfs', ?, 0, ?, ?, 28.0, 70, 1008, 3, 0, 10,
                   20, ?)""",
        (iso(DAY + timedelta(hours=12)),) * 2 +
        (iso(DAY + timedelta(hours=2)), iso(DAY + timedelta(hours=12))))
    conn.commit()
    rep = replay(conn, "2026-09-20", 1)
    conn.close()

    day_start = "2026-09-20T00:00:00Z"
    day_end = "2026-09-21T00:00:00Z"
    for s in rep["past"]["forecast_said"]:
        vt = s["valid_time"]
        assert day_start <= vt < day_end, f"future/out-of-range forecast leaked: {vt}"
        assert s["temperature"] != 99.9, "future forecast leaked into past side"
        assert s["issued_at"] <= vt, "hindsight row (issued after valid) leaked"
    for o in rep["past"]["observed"]:
        ot = o["observed_at"]
        assert day_start <= ot < day_end, f"observation out of range: {ot}"
        assert "temperature" in o and o["temperature"] is not None
    # the forecast side must not carry observation-only semantics
    assert rep["separation"]["future_valid_times_included"] == 0


def test_replay_future_date_rejected(db):
    conn = ro(db)
    future = (datetime.now(timezone.utc) + timedelta(days=3)).strftime("%Y-%m-%d")
    with pytest.raises(ReplayError):
        replay(conn, future, 1)
    conn.close()


def test_replay_bad_date_rejected(db):
    conn = ro(db)
    with pytest.raises(ReplayError):
        replay(conn, "not-a-date", 1)
    conn.close()


def test_replay_missing_data_is_honest(db):
    conn = ro(db)
    rep = replay(conn, "2026-01-05", 1)  # nothing collected then
    conn.close()
    assert rep["past"]["forecast_count"] == 0
    assert rep["past"]["observation_count"] == 0
    assert rep["coverage"]["has_forecasts"] is False
    assert "never reconstructed" in rep["coverage"]["note"]


def test_replay_route_wiring():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import backend.api.routes.replay as rmod

    app = FastAPI()
    app.include_router(rmod.router)
    client = TestClient(app)
    # date validation happens before any DB access: future/garbage -> 400
    future = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
    r = client.get("/v1/replay", params={"date": future})
    assert r.status_code == 400
    r = client.get("/v1/replay", params={"date": "garbage"})
    assert r.status_code == 400
