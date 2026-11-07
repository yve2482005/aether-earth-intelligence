"""Global Weather System tests (/v1/global): search, unified weather
(collected + live-fetch paths), overview, dashboard — on a temp DB via
TestClient. Network seams (_geocode / _live_fetch) are monkeypatched:
no live network in tests.

Honesty contract pinned here: collected answers are labelled
source="collected"; remote points are source="live_fetch" with no
predictions and no stored location; ML predictions are a distinct
kind carrying their model version; missing data is null, never invented.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402

from backend.api.app import app  # noqa: E402
from backend.db import db as dbmod  # noqa: E402
import backend.api.routes.global_weather as gw  # noqa: E402

DDL = """
CREATE TABLE locations (id INTEGER PRIMARY KEY, name TEXT, latitude REAL,
  longitude REAL, timezone TEXT, country TEXT, is_coastal INTEGER,
  created_at TEXT);
CREATE TABLE weather_forecasts (id INTEGER PRIMARY KEY AUTOINCREMENT,
  location_id INTEGER, source TEXT, model TEXT, forecast_run TEXT,
  forecast_hour INTEGER, issued_at TEXT, valid_time TEXT, temperature REAL,
  feels_like REAL, humidity REAL, pressure REAL, wind_speed REAL,
  wind_direction REAL, precipitation REAL, precip_probability REAL,
  cloud_cover REAL, collected_at TEXT);
CREATE TABLE weather_observations (id INTEGER PRIMARY KEY AUTOINCREMENT,
  location_id INTEGER, station TEXT, observed_at TEXT, temperature REAL,
  humidity REAL, pressure REAL, wind_speed REAL, wind_direction REAL,
  precipitation REAL, source TEXT, quality TEXT, collected_at TEXT);
CREATE TABLE ocean_forecasts (id INTEGER PRIMARY KEY AUTOINCREMENT,
  location_id INTEGER, source TEXT, model TEXT, valid_time TEXT,
  wave_height REAL, wave_period REAL, wave_direction REAL,
  sea_surface_temperature REAL, collected_at TEXT);
CREATE TABLE alerts (alert_id TEXT PRIMARY KEY, location_id INTEGER,
  event_id TEXT, type TEXT, severity TEXT, priority INTEGER, title TEXT,
  message TEXT, source TEXT, kind TEXT, issued_at TEXT, expires_at TEXT,
  status TEXT);
CREATE TABLE ml_batch_predictions (id INTEGER PRIMARY KEY AUTOINCREMENT,
  task TEXT NOT NULL, model_version TEXT NOT NULL, location_id INTEGER,
  valid_time TEXT NOT NULL, prediction REAL NOT NULL, features_hash TEXT,
  predicted_at TEXT NOT NULL,
  UNIQUE(task, model_version, location_id, valid_time));
CREATE TABLE ingestion_runs (run_id TEXT, component TEXT, started_at TEXT,
  ended_at TEXT, status TEXT, rows_written INTEGER, error TEXT,
  created_at TEXT);
CREATE TABLE global_places (id INTEGER PRIMARY KEY, name TEXT, country TEXT,
  latitude REAL, longitude REAL, population INTEGER);
"""


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed(path: str, with_registry: bool = True) -> None:
    import sqlite3
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    conn = sqlite3.connect(path)
    conn.executescript(DDL if with_registry else
                       DDL.replace("CREATE TABLE global_places", "CREATE TABLE _unused_gp"))
    conn.execute("INSERT INTO locations VALUES (1,'Yangon',16.84,96.17,"
                 "'Asia/Yangon','Myanmar',0,'2026-01-01T00:00:00Z')")
    conn.execute("INSERT INTO locations VALUES (2,'Mandalay',21.98,96.08,"
                 "'Asia/Yangon','Myanmar',0,'2026-01-01T00:00:00Z')")
    # A bare locations row CLOSER to the second test query point than
    # Yangon is, but with no data at all — it must never shadow Yangon.
    conn.execute("INSERT INTO locations VALUES (3,'Bare Point',16.86,96.19,"
                 "'Asia/Yangon','Myanmar',0,'2026-01-01T00:00:00Z')")
    run = _iso(now)
    fc = ("INSERT INTO weather_forecasts (location_id,source,model,"
          "forecast_run,forecast_hour,issued_at,valid_time,temperature,"
          "humidity,pressure,wind_speed,wind_direction,precipitation,"
          "precip_probability,cloud_cover,collected_at) VALUES "
          "(?,'open-meteo',?,?,0,?,?,?,?,1010,5,180,?,0.2,40,?)")
    for loc, temp0 in ((1, 30.0), (2, 33.0)):
        for model in ("gfs_seamless", "ecmwf_ifs"):
            for h in range(-5, 55):  # past + next 54 h
                vt = _iso(now + timedelta(hours=h))
                conn.execute(fc, (loc, model, run, run, vt, temp0 + (h % 5),
                                  80, 0.5 if h % 3 == 0 else 0.0, run))
    conn.execute(
        "INSERT INTO weather_observations (location_id,station,observed_at,"
        "temperature,humidity,pressure,wind_speed,source,quality,collected_at)"
        " VALUES (1,'YG',?,?,80,1011,4,'metar','good',?)",
        (_iso(now - timedelta(minutes=30)), 31.5, _iso(now)))
    conn.execute(
        "INSERT INTO ocean_forecasts (location_id,source,model,valid_time,"
        "wave_height,wave_period,wave_direction,sea_surface_temperature,"
        "collected_at) VALUES (1,'open-meteo-marine','marine',?,1.2,6.0,"
        "180.0,29.0,?)", (_iso(now + timedelta(hours=1)), _iso(now)))
    conn.execute(
        "INSERT INTO alerts VALUES ('a1',1,'us1','earthquake','moderate',2,"
        "'M5.2 near Yangon','msg','usgs','observed',?,?, 'active')",
        (_iso(now - timedelta(hours=1)), _iso(now + timedelta(days=1))))
    for task, ver in (("temperature", "weather_temperature_v2026_10_17_001"),
                      ("rain_prob", "weather_rain_prob_v2026_10_17_001")):
        for h in (2, 3):
            conn.execute(
                "INSERT INTO ml_batch_predictions (task,model_version,"
                "location_id,valid_time,prediction,predicted_at) VALUES "
                "(?,?,1,?,?,?)", (task, ver, _iso(now + timedelta(hours=h)),
                                  30.5 if task == "temperature" else 0.4,
                                  _iso(now)))
    if with_registry:
        # Paris exists ONLY in the registry; the registry Yangon row must
        # be deduped against the collected Yangon (same name + country).
        conn.execute("INSERT INTO global_places VALUES (1,'Paris','France',"
                     "48.85,2.35,2138551)")
        conn.execute("INSERT INTO global_places VALUES (2,'Yangon',"
                     "'Myanmar',16.80,96.10,4360000)")
        conn.execute("INSERT INTO global_places VALUES (3,'Paris',"
                     "'United States',33.66,-95.55,25171)")
    conn.execute(
        "INSERT INTO ingestion_runs VALUES ('r1','advanced_collect',?,?,"
        "'ok',1000,NULL,?)", (_iso(now - timedelta(minutes=10)), _iso(now),
                              _iso(now)))
    conn.execute(
        "INSERT INTO ingestion_runs VALUES ('r2','earthquakes',?,?,"
        "'ok',12,NULL,?)", (_iso(now - timedelta(minutes=10)), _iso(now),
                            _iso(now)))
    conn.commit()
    conn.close()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "gw.db"
    _seed(str(db_file))
    monkeypatch.setattr(dbmod, "DB_PATH", str(db_file))
    # Default: geocoding "succeeds" with no candidates unless a test
    # overrides it — tests must never hit the live network.
    monkeypatch.setattr(gw, "_geocode", lambda name: ([], None))
    return TestClient(app, raise_server_exceptions=False)


def _payload(body):
    if isinstance(body, dict) and isinstance(body.get("data"), dict):
        return body["data"]
    return body


# ---------------- search ----------------

def test_search_local_hit(client):
    resp = client.get("/v1/global/search", params={"q": "Yangon"})
    assert resp.status_code == 200, resp.text[:300]
    body = _payload(resp.json())
    first = body["results"][0]
    assert first["kind"] == "collected_location"
    assert first["collected"] is True
    assert first["name"] == "Yangon"
    assert first["location_id"] == 1


def test_search_merges_geocoding_candidates(client, monkeypatch):
    # A distinct place (not the collected Yangon itself — an exact
    # name+country duplicate would rightly be deduped away).
    cand = {"kind": "geocoding_candidate", "collected": False,
            "location_id": None, "name": "Yangon River", "country": "Myanmar",
            "admin1": "Yangon", "latitude": 16.70, "longitude": 96.20,
            "timezone": "Asia/Yangon"}
    monkeypatch.setattr(gw, "_geocode", lambda name: ([cand], None))
    body = _payload(client.get("/v1/global/search",
                               params={"q": "Yangon"}).json())
    kinds = [r["kind"] for r in body["results"]]
    assert kinds[0] == "collected_location"  # collected first
    assert "geocoding_candidate" in kinds


def test_search_any_city_via_geocoding(client, monkeypatch):
    cand = {"kind": "geocoding_candidate", "collected": False,
            "location_id": None, "name": "Ulaanbaatar", "country": "Mongolia",
            "admin1": None, "latitude": 47.92, "longitude": 106.92,
            "timezone": "Asia/Ulaanbaatar"}
    monkeypatch.setattr(gw, "_geocode", lambda name: ([cand], None))
    body = _payload(client.get("/v1/global/search",
                               params={"q": "Ulaanbaatar"}).json())
    assert len(body["results"]) == 1
    assert body["results"][0]["collected"] is False
    assert body["results"][0]["country"] == "Mongolia"


def test_search_geocoding_failure_is_honest(client, monkeypatch):
    monkeypatch.setattr(gw, "_geocode", lambda name: ([], "boom"))
    resp = client.get("/v1/global/search", params={"q": "Nowhereville"})
    assert resp.status_code == 200
    body = _payload(resp.json())
    assert body["results"] == []
    assert body["geocoding_error"] == "boom"
    # ...and a local hit still answers when geocoding is down:
    body2 = _payload(client.get("/v1/global/search",
                                params={"q": "Mandalay"}).json())
    assert body2["results"][0]["name"] == "Mandalay"
    assert body2["geocoding_error"] == "boom"


def test_search_registry_hit_and_population_order(client):
    # "Paris" is not collected and geocoding is stubbed empty: both hits
    # must come from the GeoNames registry, biggest city first.
    body = _payload(client.get("/v1/global/search",
                               params={"q": "Paris"}).json())
    assert len(body["results"]) == 2
    first = body["results"][0]
    assert first["kind"] == "global_registry"
    assert first["source"] == "global_registry"
    assert first["collected"] is False
    assert first["country"] == "France"
    assert first["population"] == 2138551
    assert body["results"][1]["country"] == "United States"


def test_search_registry_dedupes_collected(client):
    body = _payload(client.get("/v1/global/search",
                               params={"q": "Yangon"}).json())
    kinds = [r["kind"] for r in body["results"]]
    assert kinds == ["collected_location"]  # registry Yangon suppressed


def test_search_and_overview_without_registry_table(tmp_path, monkeypatch):
    db_file = tmp_path / "gw_noreg.db"
    _seed(str(db_file), with_registry=False)
    monkeypatch.setattr(dbmod, "DB_PATH", str(db_file))
    monkeypatch.setattr(gw, "_geocode", lambda name: ([], None))
    bare = TestClient(app, raise_server_exceptions=False)
    resp = bare.get("/v1/global/search", params={"q": "Paris"})
    assert resp.status_code == 200  # feature-detected absence, no crash
    assert _payload(resp.json())["results"] == []
    ov = _payload(bare.get("/v1/global/overview").json())
    assert ov["coverage"]["cities_registered"] is None
    assert ov["coverage"]["collected_locations"] == 3


# ---------------- weather: collected path ----------------

def test_weather_collected_yangon(client):
    resp = client.get("/v1/global/weather",
                      params={"lat": 16.84, "lon": 96.17})
    assert resp.status_code == 200, resp.text[:300]
    b = _payload(resp.json())
    assert b["source"] == "collected"
    assert b["location"]["id"] == 1
    assert b["location"]["distance_km"] <= 25
    # observation is its own kind, with the seeded value
    assert b["observation"]["kind"] == "observation"
    assert b["observation"]["temperature"] == 31.5
    # forecast: distinct kind, both models, 48 h hourly + daily summary
    fc = b["forecast"]
    assert fc["kind"] == "forecast"
    assert set(fc["models"]) == {"gfs_seamless", "ecmwf_ifs"}
    assert fc["forecast_run"]["gfs_seamless"]
    assert 0 < len(fc["hourly_next_48h"]) <= 48
    assert len(fc["daily"]) >= 2
    day0 = fc["daily"][0]
    assert day0["multi_model_temp_range"]["min"] is not None
    assert day0["models"]["gfs_seamless"]["temp_max"] is not None
    # ML predictions: separate kind, labelled, version carried
    pred = b["predictions"]
    assert pred["kind"] == "ml_prediction"
    assert "not observations" in pred["label"]
    assert "weather_temperature_v2026_10_17_001" in pred["model_versions"]
    assert pred["tasks"]["temperature"][0]["prediction"] == 30.5
    # alerts + ocean + staleness
    assert len(b["alerts"]) == 1 and b["alerts"][0]["kind"] == "alert"
    assert b["ocean"]["kind"] == "ocean_forecast"
    assert b["ocean"]["latest"]["wave_height"] == 1.2
    assert b["staleness"]["newest_data_at"] is not None
    assert b["staleness"]["data_age_min"] is not None


def test_weather_prefers_location_with_data(client):
    # Query exactly at the bare row (16.86, 96.19): Yangon (~3 km away,
    # with data) must win over the nearer data-less row.
    b = _payload(client.get("/v1/global/weather",
                            params={"lat": 16.86, "lon": 96.19}).json())
    assert b["source"] == "collected"
    assert b["location"]["id"] == 1
    assert b["forecast"] is not None


def test_weather_missing_pieces_are_null(client):
    # Mandalay has forecasts only: no observation, no predictions,
    # no alerts, no ocean — all must be null/empty, never invented.
    b = _payload(client.get("/v1/global/weather",
                            params={"lat": 21.98, "lon": 96.08}).json())
    assert b["source"] == "collected"
    assert b["location"]["id"] == 2
    assert b["observation"] is None
    assert b["predictions"] is None
    assert b["alerts"] == []
    assert b["ocean"] is None
    assert b["forecast"]["kind"] == "forecast"


# ---------------- weather: live-fetch path ----------------

def _canned_live(lat, lon):
    now = datetime.now(timezone.utc).replace(minute=0, second=0,
                                             microsecond=0)
    rows = []
    for h in range(-1, 72):
        rows.append({
            "valid_time": _iso(now + timedelta(hours=h)),
            "temperature": 12.0 + (h % 6), "humidity": 70.0,
            "pressure": 1015.0, "wind_speed": 3.0, "wind_direction": 90.0,
            "precipitation": 0.0, "precip_probability": 0.1,
            "cloud_cover": 20.0})
    return {"models": {"gfs_seamless": rows, "ecmwf_ifs": list(rows)},
            "current": {"valid_time": _iso(now), "temperature": 12.5},
            "fetched_at": _iso(now)}


def test_weather_live_fetch_remote(client, monkeypatch):
    monkeypatch.setattr(gw, "_live_fetch", _canned_live)
    resp = client.get("/v1/global/weather",
                      params={"lat": -25.0, "lon": -140.0})
    assert resp.status_code == 200, resp.text[:300]
    b = _payload(resp.json())
    assert b["source"] == "live_fetch"
    assert b["location"] is None
    assert b["predictions"] is None  # no ML predictions off the collected grid
    assert b["alerts"] == []
    assert b["forecast"]["kind"] == "forecast"
    assert set(b["forecast"]["models"]) == {"gfs_seamless", "ecmwf_ifs"}
    assert len(b["forecast"]["hourly_next_48h"]) > 0
    # live current conditions are labelled as model-derived, not an observation
    assert b["observation"]["kind"] == "current_conditions"
    assert "not a station observation" in b["observation"]["note"]
    assert "not stored" in b["note"]


def test_weather_live_fetch_failure_is_502(client, monkeypatch):
    def boom(lat, lon):
        raise RuntimeError("upstream down")
    monkeypatch.setattr(gw, "_live_fetch", boom)
    resp = client.get("/v1/global/weather",
                      params={"lat": -25.0, "lon": -140.0})
    assert resp.status_code == 502


# ---------------- overview + dashboard ----------------

def test_overview_counts_and_coverage(client):
    b = _payload(client.get("/v1/global/overview").json())
    assert b["coverage"]["collected_locations"] == 3
    assert b["coverage"]["cities_registered"] == 3  # registry rows seeded
    assert len(b["locations"]) == 3  # nothing hardcoded: fixture truth
    on_demand = b["coverage"]["on_demand"]
    assert on_demand["available"] is True
    assert "any coordinates" in on_demand["scope"]
    pc = b["prediction_coverage"]
    assert pc["locations_with_predictions"] == 1
    assert pc["collected_locations"] == 3
    assert pc["fraction"] == round(1 / 3, 3)
    assert b["active_alert_count"] == 1
    comps = {c["component"]: c for c in b["collection_status"]}
    assert comps["advanced_collect"]["status"] == "ok"
    yangon = next(d for d in b["locations"] if d["name"] == "Yangon")
    assert yangon["has_predictions"] is True
    mandalay = next(d for d in b["locations"] if d["name"] == "Mandalay")
    assert mandalay["has_predictions"] is False


def test_dashboard_renders(client):
    resp = client.get("/v1/global/dashboard")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    body = resp.text
    assert "Yangon" in body and "Mandalay" in body
    assert "Collected locations: <b>3</b>" in body
    assert "any city or coordinates worldwide" in body
    assert "M5.2 near Yangon" in body  # active alert surfaced
    assert "/v1/global/weather" in body  # lookup form wired to the API
    assert "http://" not in body and "https://" not in body  # no external assets


def test_read_only_surface(client):
    for path in ("/v1/global/search", "/v1/global/weather",
                 "/v1/global/overview", "/v1/global/dashboard"):
        assert client.post(path).status_code == 405, path
        assert client.put(path).status_code == 405, path
        assert client.delete(path).status_code == 405, path
