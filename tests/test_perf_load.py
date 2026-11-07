"""Perf tests: load + chaos (§§79-80) — concurrent requests, burst idempotency,
source-failure -> circuit breaker -> honest degraded status.

What is NOT tested here (documented in docs/PERFORMANCE.md): multi-host,
adversarial throughput, >1M-record map benchmarks on this VM.
"""
from __future__ import annotations

import sqlite3
import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.routes import perf as perf_routes
from backend.perf import ids, metrics, parallel
from backend.perf.parallel import ParallelCollector, SourceJob

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
def client(tmp_path):
    db = str(tmp_path / "load.db")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.executescript(DDL)
    conn.execute(
        "INSERT INTO locations (id,name,latitude,longitude,timezone,country,created_at)"
        " VALUES (1,'Yangon',16.84,96.17,'Asia/Yangon','Myanmar','2026-01-01T00:00:00Z')")
    conn.execute(
        """INSERT INTO weather_forecasts (location_id,source,model,forecast_run,
            forecast_hour,issued_at,valid_time,temperature,collected_at)
           VALUES (1,'open-meteo','gfs','2026-10-05T00:00:00Z',0,
            '2026-10-05T00:00:00Z','2026-10-05T01:00:00Z',30.5,'2026-10-05T00:00:00Z')""")
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


def test_concurrent_home_requests(client):
    metrics.reset()
    errors = []
    latencies = []

    def _hit():
        try:
            t0 = time.monotonic()
            r = client.get("/v1/home/1")
            latencies.append((time.monotonic() - t0) * 1000.0)
            assert r.status_code == 200
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=_hit) for _ in range(20)]
    t0 = time.monotonic()
    [t.start() for t in threads]
    [t.join() for t in threads]
    dt = time.monotonic() - t0
    assert not errors, errors[:3]
    assert dt < 30
    # all responses valid; record the real measured distribution
    latencies.sort()
    p95 = latencies[int(0.95 * len(latencies)) - 1] if latencies else None
    metrics.record("api", "home_bundle_concurrent_20", p95 or 0)


def test_event_burst_ingest_idempotent(tmp_path):
    db = str(tmp_path / "burst.db")
    init = sqlite3.connect(db)
    init.row_factory = sqlite3.Row
    init.execute("CREATE TABLE ev (id INTEGER PRIMARY KEY, h TEXT UNIQUE, n INTEGER)")
    init.commit()
    init.close()
    results = []
    lock = threading.Lock()

    def _ingest():
        # per-thread connection (§32); the UNIQUE(h) constraint is the dedup guard
        c = sqlite3.connect(db, timeout=30)
        c.row_factory = sqlite3.Row
        h = ids.quake_identity("usgs", "usB", "2026-10-05T00:00:00Z", 7.0, 16.8, 96.1)
        try:
            ok = ids.insert_once(c, "ev", "h", h, ["n"], [1])
            with lock:
                results.append(ok)
        except sqlite3.IntegrityError:
            # lost the race on UNIQUE(h): another thread ingested it first —
            # still idempotent (zero duplicates), this is the honest outcome
            with lock:
                results.append(False)
        finally:
            c.close()

    threads = [threading.Thread(target=_ingest) for _ in range(20)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    check = sqlite3.connect(db)
    check.row_factory = sqlite3.Row
    n = check.execute("SELECT COUNT(*) c FROM ev").fetchone()["c"]
    check.close()
    assert n == 1, f"burst created {n} rows for one event"
    assert sum(1 for r in results if r is True) == 1


def test_source_failure_opens_circuit_and_reports_degraded():
    pc = ParallelCollector(max_workers=2)

    def _down():
        raise ConnectionError("source unreachable (simulated)")

    for _ in range(3):
        pc.run_all([SourceJob(name="chaos_src", fn=_down,
                              failure_threshold=3, cooldown_seconds=60)])
    res = pc.run_all([SourceJob(name="chaos_src", fn=lambda: {"ok": True},
                                failure_threshold=3, cooldown_seconds=60)])
    assert res["chaos_src"]["ok"] is False
    assert res["chaos_src"].get("skipped_circuit_open") is True
    assert "circuit breaker open" in res["chaos_src"]["error"]
    assert pc.states()["chaos_src"] == "open"


def test_partial_failure_reports_per_source_honestly():
    pc = ParallelCollector(max_workers=3)

    def _a():
        return {"ok": True, "rows": 5}

    def _b():
        raise TimeoutError("slow source (simulated)")

    res = pc.run_all([SourceJob(name="up", fn=_a),
                      SourceJob(name="down", fn=_b, timeout_seconds=1)])
    assert res["up"]["ok"] is True
    assert res["down"]["ok"] is False
    # honest per-source outcomes: no aggregate "all ok" claim
    assert not all(r["ok"] for r in res.values())
