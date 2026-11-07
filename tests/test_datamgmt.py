"""Data management tests (/v1/datamgmt): inventory, row browsing,
quality, export, maintenance — on a temp DB via TestClient.

Also pins the read-only contract: no DELETE/PUT/PATCH surface exists
under /v1/datamgmt (training data is never deleted).
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402

from backend.api.app import app  # noqa: E402
from backend.db import db as dbmod  # noqa: E402
import backend.api.routes.datamgmt as datamgmt_routes  # noqa: E402

DDL = """
CREATE TABLE locations (id INTEGER PRIMARY KEY, name TEXT, latitude REAL,
  longitude REAL, created_at TEXT);
CREATE TABLE weather_forecasts (id INTEGER PRIMARY KEY AUTOINCREMENT,
  location_id INTEGER, source TEXT, model TEXT, forecast_run TEXT,
  forecast_hour INTEGER, issued_at TEXT, valid_time TEXT, temperature REAL,
  humidity REAL, pressure REAL, wind_speed REAL, precipitation REAL,
  precip_probability REAL, cloud_cover REAL, collected_at TEXT);
CREATE TABLE weather_observations (id INTEGER PRIMARY KEY AUTOINCREMENT,
  location_id INTEGER, station TEXT, observed_at TEXT, temperature REAL,
  humidity REAL, pressure REAL, wind_speed REAL, precipitation REAL,
  source TEXT, quality TEXT, collected_at TEXT);
CREATE TABLE earthquake_events (canonical_event_id TEXT PRIMARY KEY,
  source TEXT, origin_time TEXT, latitude REAL, longitude REAL, depth REAL,
  magnitude REAL, magnitude_type TEXT, region TEXT, tsunami_flag INTEGER,
  created_at TEXT);
CREATE TABLE tsunami_messages (id INTEGER PRIMARY KEY AUTOINCREMENT,
  tsunami_event_id TEXT, message_type TEXT, raw_text TEXT, issue_time TEXT,
  received_at TEXT, source_url TEXT, version INTEGER);
CREATE TABLE alerts (alert_id TEXT PRIMARY KEY, location_id INTEGER,
  event_id TEXT, type TEXT, severity TEXT, priority INTEGER, title TEXT,
  message TEXT, source TEXT, kind TEXT, issued_at TEXT, expires_at TEXT,
  status TEXT);
CREATE TABLE ml_batch_predictions (id INTEGER PRIMARY KEY AUTOINCREMENT,
  task TEXT NOT NULL, model_version TEXT NOT NULL, location_id INTEGER,
  valid_time TEXT NOT NULL, prediction REAL NOT NULL, features_hash TEXT,
  predicted_at TEXT NOT NULL,
  UNIQUE(task, model_version, location_id, valid_time));
"""


def _seed(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(DDL)
    conn.execute("INSERT INTO locations VALUES (1,'Yangon',16.8,96.15,"
                 "'2026-01-01T00:00:00Z')")
    fc = ("INSERT INTO weather_forecasts (location_id,source,model,"
          "forecast_run,forecast_hour,issued_at,valid_time,temperature,"
          "humidity,pressure,wind_speed,precipitation,precip_probability,"
          "cloud_cover,collected_at) VALUES (1,'open-meteo','gfs',"
          "'2026-10-17T00:00:00Z',?,?,?,?,80,1010,5,0,0.1,40,"
          "'2026-10-17T00:05:00Z')")
    # 4 distinct valid_times + 1 duplicate natural key; one NULL temperature
    rows = [(1, "2026-10-17T00:00:00Z", "2026-10-17T01:00:00Z", 30.0),
            (2, "2026-10-17T00:00:00Z", "2026-10-17T02:00:00Z", 31.0),
            (3, "2026-10-17T00:00:00Z", "2026-10-17T03:00:00Z", None),
            (4, "2026-10-17T00:00:00Z", "2026-10-17T04:00:00Z", 29.0),
            (5, "2026-10-17T00:00:00Z", "2026-10-17T04:00:00Z", 28.5)]
    for fh, issued, vt, temp in rows:
        conn.execute(fc, (fh, issued, vt, temp))
    conn.execute("INSERT INTO weather_observations (location_id,station,"
                 "observed_at,temperature,source) VALUES (1,'YG','2026-10-17T01:00:00Z',30.1,'metar')")
    conn.execute("INSERT INTO earthquake_events VALUES ('us1','usgs',"
                 "'2026-10-17T01:00:00Z',14.0,-92.8,10.0,5.2,'mww','Mexico',0,"
                 "'2026-10-17T01:05:00Z')")
    conn.execute("INSERT INTO alerts VALUES ('a1',1,'us1','earthquake',"
                 "'moderate',2,'M5.2','msg','usgs','observed',"
                 "'2026-10-17T01:10:00Z',NULL,'active')")
    conn.execute("INSERT INTO ml_batch_predictions (task,model_version,"
                 "location_id,valid_time,prediction,predicted_at) VALUES "
                 "('temperature','weather_temperature_v2026_10_10_001',1,"
                 "'2026-10-17T02:00:00Z',30.5,'2026-10-17T01:00:00Z')")
    conn.commit()
    conn.close()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "dm.db"
    _seed(str(db_file))
    monkeypatch.setattr(dbmod, "DB_PATH", str(db_file))
    monkeypatch.setattr(datamgmt_routes, "DB_PATH", str(db_file))
    return TestClient(app, raise_server_exceptions=False)


def _payload(body):
    if isinstance(body, dict) and isinstance(body.get("data"), dict):
        return body["data"]
    return body


def test_overview_lists_tables(client):
    resp = client.get("/v1/datamgmt/overview")
    assert resp.status_code == 200, resp.text[:200]
    body = _payload(resp.json())
    names = [t["table"] for t in body["tables"]]
    assert "weather_forecasts" in names and "alerts" in names
    assert body["table_count"] == len(names) == 7
    fc = next(t for t in body["tables"] if t["table"] == "weather_forecasts")
    assert fc["row_count"] == 5
    assert fc["time_column"] == "valid_time"
    assert fc["time_min"] == "2026-10-17T01:00:00Z"
    assert fc["time_max"] == "2026-10-17T04:00:00Z"
    colnames = [c["name"] for c in fc["columns"]]
    assert "temperature" in colnames


def test_rows_paging_and_ordering(client):
    r1 = client.get("/v1/datamgmt/tables/weather_forecasts/rows",
                    params={"limit": 2, "offset": 0, "order_by": "valid_time"})
    assert r1.status_code == 200
    b1 = _payload(r1.json())
    assert b1["total"] == 5 and len(b1["rows"]) == 2
    r2 = client.get("/v1/datamgmt/tables/weather_forecasts/rows",
                    params={"limit": 2, "offset": 4, "order_by": "valid_time"})
    b2 = _payload(r2.json())
    assert len(b2["rows"]) == 1
    times = [r["valid_time"] for r in b1["rows"]]
    assert times == sorted(times)


def test_rows_rejects_bad_order_by_and_unknown_table(client):
    bad = client.get("/v1/datamgmt/tables/weather_forecasts/rows",
                     params={"order_by": "no_such_col"})
    assert bad.status_code == 400
    inj = client.get("/v1/datamgmt/tables/weather_forecasts/rows",
                     params={"order_by": "valid_time; DROP TABLE alerts"})
    assert inj.status_code == 400
    missing = client.get("/v1/datamgmt/tables/nope/rows")
    assert missing.status_code == 404


def test_export_returns_csv(client):
    resp = client.get("/v1/datamgmt/export/weather_forecasts")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    lines = resp.text.strip().splitlines()
    assert lines[0].startswith("id,location_id,")
    assert len(lines) == 6  # header + 5 rows
    assert "attachment" in resp.headers["content-disposition"]


def test_export_unknown_table_404(client):
    assert client.get("/v1/datamgmt/export/nope").status_code == 404
    assert client.get("/v1/datamgmt/export/weather_forecasts;DROP").status_code in (404, 400)


def test_quality_returns_datasets(client):
    resp = client.get("/v1/datamgmt/quality")
    assert resp.status_code == 200
    ds = _payload(resp.json())["datasets"]
    assert set(ds) == {"weather_forecasts", "weather_observations",
                       "earthquake_events", "tsunami_messages", "alerts",
                       "ml_batch_predictions"}
    fc = ds["weather_forecasts"]
    assert fc["present"] is True and fc["row_count"] == 5
    assert fc["duplicate_natural_key_rows"] == 1
    assert fc["null_rates"]["temperature"] == 0.2
    assert fc["latest_timestamp"] == "2026-10-17T04:00:00Z"
    assert ds["tsunami_messages"]["row_count"] == 0
    assert ds["tsunami_messages"]["latest_timestamp"] is None


def test_maintenance_integrity_and_analyze(client):
    r = client.post("/v1/datamgmt/maintenance/integrity")
    assert r.status_code == 200
    body = _payload(r.json())
    assert body["ok"] is True and body["integrity_check"] == ["ok"]
    assert body["foreign_key_violations"] == []
    r2 = client.post("/v1/datamgmt/maintenance/analyze")
    assert r2.status_code == 200 and _payload(r2.json())["analyze"] == "ok"


def test_maintenance_vacuum(client):
    r = client.post("/v1/datamgmt/maintenance/vacuum")
    assert r.status_code == 200, r.text[:200]
    body = _payload(r.json())
    assert body["vacuum"] == "ok"
    assert body["size_bytes_after"] > 0


def test_backups_endpoint_honest(client):
    r = client.get("/v1/datamgmt/backups")
    assert r.status_code == 200
    body = _payload(r.json())
    assert isinstance(body["local_backups"], list)
    assert "note" in body


def test_dashboard_page(client):
    r = client.get("/v1/datamgmt/dashboard")
    assert r.status_code == 200
    assert "AETHER" in r.text and "/v1/datamgmt/overview" in r.text


def test_no_mutating_surface(client):
    # Read-only contract: no DELETE/PUT/PATCH anywhere under /v1/datamgmt,
    # and no POST outside /maintenance/*.
    assert client.delete("/v1/datamgmt/tables/weather_forecasts/rows").status_code == 405
    assert client.put("/v1/datamgmt/overview").status_code == 405
    assert client.patch("/v1/datamgmt/quality").status_code == 405
    assert client.post("/v1/datamgmt/tables/weather_forecasts/rows").status_code == 405
