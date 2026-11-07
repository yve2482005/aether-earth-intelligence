"""Perf tests: perf API routes (§§14-15, 18, 58, 72) via TestClient.

Mounts backend.api.routes.perf.router directly (app.py wiring is one line
owned by workstream E). Uses a temp SQLite DB via monkeypatched _conn.
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.routes import perf as perf_routes

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
def client(tmp_path, monkeypatch):
    db = str(tmp_path / "api.db")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.executescript(DDL)
    conn.execute(
        "INSERT INTO locations (id,name,latitude,longitude,timezone,country,created_at)"
        " VALUES (1,'Yangon',16.84,96.17,'Asia/Yangon','Myanmar','2026-01-01T00:00:00Z')")
    conn.execute(
        """INSERT INTO weather_forecasts (location_id,source,model,forecast_run,
            forecast_hour,issued_at,valid_time,temperature,aqi,collected_at)
           VALUES (1,'open-meteo','gfs','2026-10-05T00:00:00Z',0,
            '2026-10-05T00:00:00Z','2026-10-05T01:00:00Z',30.5,42,'2026-10-05T00:00:00Z')""")
    # a recent quake inside the bbox for map tests (origin within 72h)
    conn.execute(
        """INSERT INTO earthquake_events (canonical_event_id,source,source_event_ids,
            origin_time,latitude,longitude,magnitude,region,created_at)
           VALUES ('q1','usgs','["us1"]',datetime('now','-1 hours'),16.9,96.2,6.0,
            'Myanmar',datetime('now','-1 hours'))""")
    conn.commit()

    def _conn():
        c = sqlite3.connect(db)
        c.row_factory = sqlite3.Row
        try:
            yield c
        finally:
            c.close()

    app = FastAPI()
    app.include_router(perf_routes.router)
    app.dependency_overrides[perf_routes._conn] = _conn
    yield TestClient(app)
    conn.close()


def test_home_bundle_summary(client):
    r = client.get("/v1/home/1")
    assert r.status_code == 200
    b = r.json()
    assert b["location"]["name"] == "Yangon"
    assert len(b["weather"]["hourly"]) == 1
    assert b["level"] == "summary"
    assert b["perf"]["latency_ms"] >= 0


def test_home_bundle_cached_second_hit(client):
    client.get("/v1/home/1")
    r = client.get("/v1/home/1")
    assert r.json()["perf"]["served_from_cache"] is True


def test_home_bundle_404(client):
    assert client.get("/v1/home/999").status_code == 404


def test_map_events_clustered(client):
    r = client.get("/v1/map/events", params={"bbox": "90,10,100,22", "zoom": 5})
    assert r.status_code == 200
    j = r.json()
    assert j["mode"] == "clustered_res5"
    assert j["count"] <= 2000
    assert j["events_considered"] >= 1


def test_map_events_individual_high_zoom(client):
    r = client.get("/v1/map/events", params={"bbox": "90,10,100,22", "zoom": 10})
    j = r.json()
    assert j["mode"] == "individual"
    assert any(f.get("event_id") == "q1" for f in j["features"])


def test_map_events_bbox_required(client):
    assert client.get("/v1/map/events").status_code == 422


def test_map_events_hard_cap(client):
    r = client.get("/v1/map/events", params={"bbox": "-180,-90,180,90", "zoom": 2})
    j = r.json()
    assert j["cap"] == 2000 and j["count"] <= 2000


def test_perf_metrics_endpoint(client):
    r = client.get("/v1/perf/metrics")
    assert r.status_code == 200 and "stages" in r.json()


def test_perf_schedule_endpoint(client):
    r = client.get("/v1/perf/schedule")
    j = r.json()
    assert "open-meteo" in [s["source"] for s in j["sources"]]
    assert isinstance(j["due_now"], list)


def test_perf_circuits_endpoint(client):
    assert client.get("/v1/perf/circuits").status_code == 200


def test_notify_enqueue_never_waits(client):
    r = client.post("/v1/perf/notify/enqueue",
                    json={"lane": "critical", "title": "T", "body": "B"})
    assert r.status_code == 200 and r.json()["queued_id"] > 0
    d = client.get("/v1/perf/notify/queue").json()["depth"]
    assert d["critical"] >= 1
