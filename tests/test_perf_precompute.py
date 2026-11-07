"""Perf tests: home bundle precomputation (§§14-15, 47-48). Hermetic."""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest

from backend.perf import precompute


DDL = """
CREATE TABLE locations (id INTEGER PRIMARY KEY, name TEXT, latitude REAL,
  longitude REAL, timezone TEXT, country TEXT, created_at TEXT);
CREATE TABLE weather_forecasts (id INTEGER PRIMARY KEY AUTOINCREMENT,
  location_id INTEGER, source TEXT, model TEXT, forecast_run TEXT,
  forecast_hour INTEGER, issued_at TEXT, valid_time TEXT, temperature REAL,
  feels_like REAL, humidity REAL, pressure REAL, wind_speed REAL,
  wind_direction REAL, precipitation REAL, precip_probability REAL,
  cloud_cover REAL, aqi REAL, pm2_5 REAL, pm10 REAL, collected_at TEXT);
CREATE TABLE weather_daily (id INTEGER PRIMARY KEY AUTOINCREMENT,
  location_id INTEGER, source TEXT, model TEXT, date TEXT, sunrise TEXT,
  sunset TEXT, collected_at TEXT);
CREATE TABLE alerts (alert_id TEXT PRIMARY KEY, location_id INTEGER,
  event_id TEXT, type TEXT, severity TEXT, priority INTEGER, title TEXT,
  message TEXT, source TEXT, kind TEXT, issued_at TEXT, expires_at TEXT,
  status TEXT, dedup_key TEXT);
CREATE TABLE earthquake_events (canonical_event_id TEXT PRIMARY KEY,
  source TEXT, source_event_ids TEXT, origin_time TEXT, latitude REAL,
  longitude REAL, depth REAL, magnitude REAL, magnitude_type TEXT, region TEXT,
  status TEXT, review_status TEXT, felt_count INTEGER, mmi REAL,
  alert_level TEXT, tsunami_flag INTEGER, shakemap_available INTEGER,
  pager_available INTEGER, source_url TEXT, update_time TEXT, created_at TEXT);
CREATE TABLE tsunami_events (tsunami_event_id TEXT PRIMARY KEY, source TEXT,
  earthquake_event_id TEXT, message_type TEXT, severity TEXT, affected_region TEXT,
  issue_time TEXT, arrival_time TEXT, status TEXT);
CREATE TABLE system_health (component TEXT, status TEXT, checked_at TEXT,
  latency_ms REAL, records INTEGER, message TEXT);
"""


@pytest.fixture()
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(DDL)
    conn.execute(
        "INSERT INTO locations (id,name,latitude,longitude,timezone,country,created_at)"
        " VALUES (1,'Yangon',16.84,96.17,'Asia/Yangon','Myanmar','2026-01-01T00:00:00Z')")
    conn.execute(
        """INSERT INTO weather_forecasts (location_id,source,model,forecast_run,
            forecast_hour,issued_at,valid_time,temperature,aqi,pm2_5,collected_at)
           VALUES (1,'open-meteo','gfs','2026-10-05T00:00:00Z',0,
            '2026-10-05T00:00:00Z','2026-10-05T01:00:00Z',30.5,55,25,'2026-10-05T00:00:00Z')""")
    conn.execute(
        """INSERT INTO alerts (alert_id,location_id,type,severity,priority,title,
            message,source,kind,issued_at,status)
           VALUES ('a1',1,'EARTHQUAKE','warning',1,'M6 test','shaking','usgs',
            'official','2026-10-05T00:00:00Z','active')""")
    # Quake date is relative to now: build_home_bundle only counts quakes
    # from the trailing 30 days, so a fixed 2026-10-04 date ages out and
    # the proximity list comes back empty (failed 2026-11-07 as a time-bomb).
    qdate = (datetime.now(timezone.utc) - timedelta(days=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        """INSERT INTO earthquake_events (canonical_event_id,source,source_event_ids,
            origin_time,latitude,longitude,magnitude,region,created_at)
           VALUES ('q1','usgs','["us1"]',?,16.9,96.2,6.0,
            'Myanmar',?)""", (qdate, qdate))
    conn.execute(
        """INSERT INTO system_health (component,status,checked_at)
           VALUES ('usgs-poll','ok','2026-10-05T00:00:00Z'),
                  ('openmeteo-collect','error','2026-10-05T00:00:00Z')""")
    conn.commit()
    yield conn
    conn.close()


def test_bundle_has_all_parts(db):
    b = precompute.build_home_bundle(db, 1, level="summary")
    assert b["location"]["name"] == "Yangon"
    assert len(b["weather"]["hourly"]) == 1
    assert b["aqi"]["pm2_5"] == 25
    assert len(b["alerts"]) == 1
    assert b["quake_proximity"]["events_within_2000km"][0]["canonical_event_id"] == "q1"
    assert b["quake_proximity"]["nearest_km"] is not None
    assert b["quake_proximity"]["nearest_km"] < 30  # ~10km away: real check
    assert "freshness" in b and "confidence" in b and "provenance" in b


def test_freshness_honesty(db):
    b = precompute.build_home_bundle(db, 1)
    assert b["freshness"]["weather"]["status"] == "could_not_verify"  # openmeteo error
    assert b["freshness"]["earthquake"]["status"] == "verified"        # usgs ok


def test_bundle_cache_store_and_reuse(db):
    b1 = precompute.build_home_bundle(db, 1)
    precompute.store_bundle(db, 1, "summary", b1, valid_seconds=600)
    got = precompute.get_bundle(db, 1, "summary")
    assert got is not None and got["location"]["name"] == "Yangon"
    precompute.store_bundle(db, 1, "summary", b1, valid_seconds=0.01)
    time.sleep(0.02)
    assert precompute.get_bundle(db, 1, "summary") is None  # expired: rebuilt


def test_bundle_missing_location(db):
    with pytest.raises(KeyError):
        precompute.build_home_bundle(db, 999)
