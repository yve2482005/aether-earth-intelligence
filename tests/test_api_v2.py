"""v2 platform tests (workstream E): envelope, pagination, rate limiting, auth
hook, v2 DB migration idempotency, retention pruning.

Existing contract tests are untouched; these only cover new v2 behavior.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
ROOT = os.path.join(os.path.dirname(__file__), "..")

from backend.api.auth import (  # noqa: E402
    API_KEY_ENV,
    auth_enabled,
    check_key,
    require_api_key,
)
from backend.api.envelope import envelope, unavailable_envelope  # noqa: E402
from backend.api.pagination import MAX_PAGE_SIZE, paginate  # noqa: E402
from backend.db import db as dbmod  # noqa: E402
from backend.db import migrate_v2  # noqa: E402

V2_TABLES = ("alert_revisions", "notification_history", "notification_deliveries",
             "incidents", "devices", "alert_preferences", "data_quality",
             "ingestion_runs")


# ---------------------------------------------------------------- envelope

def test_envelope_shape():
    e = envelope({"x": 1}, source="usgs", model_version="mv1",
                 confidence="high", data_quality="ok", status="ok")
    assert set(e) == {"data", "source", "source_timestamp", "retrieved_at",
                      "processed_at", "model_version", "confidence",
                      "data_quality", "status"}
    assert e["data"] == {"x": 1}
    assert e["source"] == ["usgs"]
    assert e["model_version"] == "mv1"
    assert e["confidence"] == "high"
    assert e["data_quality"] == "ok"
    assert e["status"] == "ok"


def test_envelope_defaults_and_list_source():
    e = envelope([1, 2], source=["a", "b"])
    assert e["source"] == ["a", "b"]
    assert e["status"] == "ok"
    assert e["confidence"] == "unknown"
    assert e["data_quality"] == "unknown"
    assert e["retrieved_at"] and e["processed_at"]


def test_unavailable_envelope_is_honest():
    e = unavailable_envelope("source down", source="nws")
    assert e["status"] == "unavailable"
    assert e["data"] == {"message": "source down"}


# --------------------------------------------------------------- pagination

def test_paginate_math():
    p = paginate(list(range(95)), page=2, page_size=50)
    assert p["total"] == 95
    assert p["pages"] == 2
    assert len(p["items"]) == 45
    assert p["items"][0] == 50
    assert p["has_next"] is False
    assert p["has_prev"] is True


def test_paginate_caps_page_size():
    p = paginate(list(range(MAX_PAGE_SIZE + 100)), page_size=10_000)
    assert p["page_size"] == MAX_PAGE_SIZE
    assert len(p["items"]) == MAX_PAGE_SIZE


def test_paginate_bad_input_clamped():
    p = paginate([1, 2, 3], page=0, page_size=-5)
    assert p["page"] == 1
    assert p["page_size"] == 1
    assert p["items"] == [1]


def test_paginate_beyond_last_page_is_empty():
    p = paginate(list(range(10)), page=9, page_size=10)
    assert p["items"] == []
    assert p["has_next"] is False


# ------------------------------------------------------------ rate limiting

def _limited_app(rpm):
    from fastapi import FastAPI
    from backend.api.middleware import RateLimitMiddleware
    a = FastAPI()
    a.add_middleware(RateLimitMiddleware, requests_per_minute=rpm, burst=rpm)

    @a.get("/v1/ping")
    def ping():
        return {"ok": True}

    @a.get("/health")
    def h():
        return {"ok": True}

    return a


def test_rate_limiter_blocks_after_quota():
    from fastapi.testclient import TestClient
    client = TestClient(_limited_app(3), raise_server_exceptions=False)
    codes = [client.get("/v1/ping").status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    last = client.get("/v1/ping")
    assert last.status_code == 429
    assert last.json()["detail"] == "rate limit exceeded"
    assert "Retry-After" in last.headers


def test_rate_limiter_exempts_ops_paths():
    from fastapi.testclient import TestClient
    client = TestClient(_limited_app(1), raise_server_exceptions=False)
    client.get("/v1/ping")  # exhaust the single token
    for _ in range(5):
        assert client.get("/health").status_code == 200


# ------------------------------------------------------------------- auth

def _auth_app():
    from fastapi import Depends, FastAPI
    a = FastAPI()

    @a.get("/v1/admin/ping", dependencies=[Depends(require_api_key)])
    def ping():
        return {"ok": True}

    return a


def test_auth_disabled_by_default(monkeypatch):
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    assert not auth_enabled()
    assert check_key(None) is None  # open passthrough
    from fastapi.testclient import TestClient
    client = TestClient(_auth_app(), raise_server_exceptions=False)
    assert client.get("/v1/admin/ping").status_code == 200


def test_auth_enabled_paths(monkeypatch):
    monkeypatch.setenv(API_KEY_ENV, "secret-key")
    assert auth_enabled()
    from fastapi.testclient import TestClient
    client = TestClient(_auth_app(), raise_server_exceptions=False)
    assert client.get("/v1/admin/ping").status_code == 401
    assert client.get("/v1/admin/ping",
                      headers={"X-API-Key": "wrong"}).status_code == 401
    ok = client.get("/v1/admin/ping", headers={"X-API-Key": "secret-key"})
    assert ok.status_code == 200


# --------------------------------------------------------------- migration

def _table_names(conn):
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


def test_migrate_v2_creates_all_tables(tmp_path):
    p = str(tmp_path / "v2.db")
    migrate_v2.migrate(p)
    conn = dbmod.connect(p)
    try:
        names = _table_names(conn)
    finally:
        conn.close()
    for t in V2_TABLES:
        assert t in names, t


def test_migrate_v2_idempotent(tmp_path):
    p = str(tmp_path / "v2.db")
    first = migrate_v2.migrate(p)
    second = migrate_v2.migrate(p)
    assert set(first) == set(second) == set(V2_TABLES)
    conn = dbmod.connect(p)
    try:
        # no duplicate index/table creation blew up; tables still present
        assert set(V2_TABLES) <= _table_names(conn)
    finally:
        conn.close()


def test_schema_sql_init_db_includes_v2_tables(tmp_path):
    p = str(tmp_path / "s.db")
    dbmod.init_db(p)  # runs backend/db/schema.sql
    conn = dbmod.connect(p)
    try:
        names = _table_names(conn)
    finally:
        conn.close()
    for t in V2_TABLES:
        assert t in names, t


def test_existing_tables_untouched_by_migration(tmp_path):
    p = str(tmp_path / "keep.db")
    dbmod.init_db(p)
    migrate_v2.migrate(p)
    conn = dbmod.connect(p)
    try:
        # original 16 tables still there with original columns
        cols = [r[1] for r in conn.execute("PRAGMA table_info(alerts)").fetchall()]
        assert "dedup_key" in cols
        names = _table_names(conn)
        assert "weather_forecasts" in names and "alerts" in names
    finally:
        conn.close()


# ------------------------------------------------------------------ prune

def _load_prune():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "aether_prune", os.path.join(ROOT, "scripts", "prune.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _seed_forecast(conn, valid_time, forecast_hour):
    conn.execute("INSERT INTO locations (name, latitude, longitude, created_at)"
                 " VALUES ('T', 1.0, 1.0, '2026-01-01T00:00:00Z')")
    loc = conn.execute("SELECT id FROM locations").fetchone()["id"]
    conn.execute(
        """INSERT INTO weather_forecasts (location_id, source, model, forecast_run,
               forecast_hour, issued_at, valid_time, collected_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (loc, "open-meteo", "gfs", "2020-01-01T00:00:00Z", forecast_hour,
         "2020-01-01T00:00:00Z", valid_time, "2020-01-01T00:00:00Z"))
    conn.commit()


def test_prune_dry_run_default_and_apply(tmp_path):
    """Updated 2026-10-17 for the owner's never-delete rule (2026-10-16):
    weather_forecasts / weather_observations are ML training data and are
    NEVER pruned. The previous version of this test expected a 365-day
    forecast window and failed against the current policy. Prune
    dry-run/apply behaviour is now exercised on system_health (180 days),
    and the forecast table is asserted untouchable even when ancient."""
    prune_mod = _load_prune()
    p = str(tmp_path / "prune.db")
    dbmod.init_db(p)
    conn = dbmod.connect(p)
    try:
        _seed_forecast(conn, "2020-01-01T00:00:00Z", 0)   # ancient, still kept
        conn.execute(
            "INSERT INTO system_health (component, status, message, checked_at)"
            " VALUES ('old', 'ok', '', '2020-01-01T00:00:00Z')")
        conn.execute(
            "INSERT INTO system_health (component, status, message, checked_at)"
            " VALUES ('fresh', 'ok', '', '2026-10-01T00:00:00Z')")
        conn.commit()
    finally:
        conn.close()
    # training data is never even in the prune plan
    report = prune_mod.prune(apply=False, path=p)
    assert report["dry_run"] is True
    assert all(r[0] != "weather_forecasts" for r in report["plan"])
    assert all(r[0] != "weather_observations" for r in report["plan"])
    sh = [r for r in report["plan"] if r[0] == "system_health"][0]
    assert sh[2] == 1  # one stale telemetry row would be pruned
    # dry run deleted nothing
    conn = dbmod.connect(p)
    try:
        assert dbmod.table_count(conn, "weather_forecasts") == 1
        assert dbmod.table_count(conn, "system_health") == 2
    finally:
        conn.close()
    result = prune_mod.prune(apply=True, path=p)
    assert result["dry_run"] is False
    conn = dbmod.connect(p)
    try:
        # telemetry pruned, training data untouched
        assert dbmod.table_count(conn, "system_health") == 1
        assert dbmod.table_count(conn, "weather_forecasts") == 1
        row = dbmod.query_one(conn, "SELECT valid_time AS v FROM weather_forecasts")
        assert row["v"] == "2020-01-01T00:00:00Z"
    finally:
        conn.close()
