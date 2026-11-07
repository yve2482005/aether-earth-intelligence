"""Advanced collector tests: validation gate, gap detection + backfill,
run ledger, and failure isolation. All collectors are monkeypatched
fakes — NO live network. The DB is a real fixture SQLite file built
with the production schema (dbmod.init_db).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from backend.collectors import advanced
from backend.collectors.advanced import (ValidatingConnection,
                                         run_advanced_collection,
                                         value_in_range)
from backend.collectors.weather.openmeteo import INSERT_SQL as FORECAST_INSERT_SQL
from backend.common.timeutil import hour_floor, now_utc, utc_iso
from backend.db import db as dbmod

YANGON_LAT = 16.84  # backend.common.config.DEFAULT_LAT


# ---------------------------------------------------------------------------
# helpers / fakes
# ---------------------------------------------------------------------------

def insert_forecast(conn, location_id, model, valid_time, run, **vals):
    """Insert one forecast row through whatever connection is passed in
    (in production paths that is the ValidatingConnection)."""
    base = {"temperature": 30.0, "humidity": 80.0, "pressure": 1005.0,
            "wind_speed": 5.0, "precipitation": 0.0}
    base.update(vals)
    return conn.execute(FORECAST_INSERT_SQL, (
        location_id, "open-meteo", model, run, 0, run, valid_time,
        base["temperature"], None, base["humidity"], base["pressure"],
        base["wind_speed"], 90.0, base["precipitation"], 10.0, 40.0,
        10000.0, 5.0, None, run, 25.0, 8.0,
        None, None, None, None, None, None))


def add_location(db, name, lat, lon, country="Testland", coastal=0):
    conn = dbmod.connect(db)
    cur = conn.execute(
        """INSERT INTO locations (name, latitude, longitude, timezone, country,
                                  is_coastal, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (name, lat, lon, "UTC", country, coastal, "2026-01-01T00:00:00Z"))
    conn.commit()
    lid = cur.lastrowid
    conn.close()
    return lid


def ok_openmeteo(conn, location_id, lat, lon, session=None, forecast_days=7):
    return {"location_id": location_id, "models": {}, "air_quality": None}


@pytest.fixture()
def adv_db(tmp_path, monkeypatch):
    """Fixture DB + every global feed and side collector faked off."""
    db = str(tmp_path / "adv.db")
    dbmod.init_db(db)
    add_location(db, "Testburg", 10.0, 20.0)
    # global feeds -> harmless no-ops
    monkeypatch.setattr(advanced.satellite, "collect", lambda conn, **kw: {"ok": True})
    monkeypatch.setattr(advanced.radar, "collect", lambda conn, **kw: {"ok": True})
    monkeypatch.setattr(advanced.usgs, "collect", lambda conn, session=None: {"ok": True})
    monkeypatch.setattr(advanced.eq_regional, "collect", lambda conn, **kw: {"ok": True})
    monkeypatch.setattr(advanced.tsunami_src, "collect", lambda conn, session=None: {"ok": True})
    monkeypatch.setattr(advanced.tsunami_obs, "collect", lambda conn, **kw: {"ok": True})
    monkeypatch.setattr(advanced.aftershock, "get_aftershock_forecast",
                        lambda conn, session=None: {"ok": True})
    monkeypatch.setattr(advanced.alert_engine, "build_alerts", lambda conn: {"built": 0})
    # per-location side collectors -> no-ops (openmeteo set per-test)
    monkeypatch.setattr(advanced._rc.wx_obs, "collect",
                        lambda conn, lid, lat, lon, session=None: {"ok": True, "inserted": 0})
    monkeypatch.setattr(advanced._rc.ocean_engine, "collect",
                        lambda conn, lid, lat, lon, session=None: {"ok": True})
    monkeypatch.setattr(advanced._rc.openmeteo, "collect", ok_openmeteo)
    return db


# ---------------------------------------------------------------------------
# validation gate
# ---------------------------------------------------------------------------

def test_value_in_range_boundaries():
    assert value_in_range("temperature", -90.0)
    assert value_in_range("temperature", 60.0)
    assert not value_in_range("temperature", 60.1)
    assert not value_in_range("temperature", -90.1)
    assert value_in_range("humidity", 0.0) and value_in_range("humidity", 100.0)
    assert not value_in_range("humidity", 100.1)
    assert value_in_range("pressure", 800.0) and value_in_range("pressure", 1100.0)
    assert not value_in_range("pressure", 799.9)
    assert value_in_range("wind_speed", 0.0) and not value_in_range("wind_speed", -0.1)
    assert value_in_range("precipitation", 0.0)
    assert not value_in_range("precipitation", -1.0)
    assert value_in_range("temperature", None)       # NULL is not a violation
    assert not value_in_range("temperature", "hot")  # non-numeric fails
    assert not value_in_range("temperature", float("nan"))


def test_validation_gate_rejects_and_counts(adv_db, monkeypatch):
    run = utc_iso(hour_floor())
    vt = utc_iso(hour_floor(now_utc() + timedelta(hours=1)))

    def fake_collect(conn, location_id, lat, lon, session=None, forecast_days=7):
        if lat != YANGON_LAT:
            return ok_openmeteo(conn, location_id, lat, lon)
        insert_forecast(conn, location_id, "gfs_seamless", vt, run)  # valid
        bad = [{"temperature": 75.0}, {"humidity": 140.0}, {"pressure": 700.0},
               {"wind_speed": -3.0}, {"precipitation": -1.0}]
        for i, override in enumerate(bad):
            insert_forecast(conn, location_id, "gfs_seamless",
                            utc_iso(hour_floor(now_utc() + timedelta(hours=2 + i))),
                            run, **override)
        conn.commit()
        return {"location_id": location_id, "models": {}, "air_quality": None}

    monkeypatch.setattr(advanced._rc.openmeteo, "collect", fake_collect)
    # backfill=False: this test targets the collection-phase gate only
    # (the backfill re-fetch would run the same fake a second time).
    summary = run_advanced_collection(db_path=adv_db, max_workers=2, backfill=False)

    conn = dbmod.connect(adv_db)
    rows = conn.execute(
        "SELECT temperature, humidity, pressure, wind_speed, precipitation"
        " FROM weather_forecasts").fetchall()
    conn.close()
    assert len(rows) == 1, "only the valid row may be stored"
    assert rows[0]["temperature"] == 30.0
    assert summary["validation"]["rejected"] == 5
    assert summary["validation"]["by_field"] == {
        "temperature": 1, "humidity": 1, "pressure": 1,
        "wind_speed": 1, "precipitation": 1}
    assert summary["validation"]["by_location"] == {"Yangon": 5}


def test_validating_connection_passes_other_sql(tmp_path):
    db = str(tmp_path / "vc.db")
    dbmod.init_db(db)
    raw = dbmod.connect(db)
    conn = ValidatingConnection(raw)
    conn.execute("INSERT INTO locations (name, latitude, longitude, created_at)"
                 " VALUES ('X', 1.0, 2.0, '2026-01-01T00:00:00Z')")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM locations").fetchone()[0] == 1
    assert conn.rejected == 0
    raw.close()


# ---------------------------------------------------------------------------
# gap detection + backfill
# ---------------------------------------------------------------------------

def test_gap_detection_and_backfill(adv_db, monkeypatch):
    conn = dbmod.connect(adv_db)
    testburg_id = conn.execute(
        "SELECT id FROM locations WHERE name='Testburg'").fetchone()[0]
    now = now_utc()
    end = hour_floor(now)
    start = end - timedelta(hours=48)
    expected = []
    t = start
    while t < end:
        expected.append(utc_iso(t))
        t += timedelta(hours=1)
    floor = utc_iso(datetime(now.year, now.month, now.day, tzinfo=timezone.utc))
    fillable_hours = [h for h in expected if h >= floor]
    if not fillable_hours:
        pytest.skip("within the first hour of the UTC day: no fillable hour")
    missing = fillable_hours[-1]
    old_run = utc_iso(hour_floor(now - timedelta(hours=50)))
    for model in advanced.MODELS:
        for h in expected:
            if model == "gfs_seamless" and h == missing:
                continue  # the planted gap
            insert_forecast(conn, testburg_id, model, h, old_run)
    conn.commit()
    conn.close()

    calls = {"n": 0}

    def fake_collect(conn, location_id, lat, lon, session=None, forecast_days=7):
        if lat == 10.0:  # Testburg
            calls["n"] += 1
            if calls["n"] >= 2:  # backfill call fills the planted gap
                insert_forecast(conn, location_id, "gfs_seamless", missing,
                                utc_iso(hour_floor()))
                conn.commit()
        return ok_openmeteo(conn, location_id, lat, lon)

    monkeypatch.setattr(advanced._rc.openmeteo, "collect", fake_collect)
    summary = run_advanced_collection(db_path=adv_db, max_workers=2)

    assert summary["gaps"]["found"] >= 1
    assert summary["gaps"]["filled"] == 1
    assert summary["gaps"]["remaining"] == summary["gaps"]["found"] - 1
    assert "Testburg" in summary["gaps"]["backfilled_locations"]
    conn = dbmod.connect(adv_db)
    n = conn.execute(
        "SELECT COUNT(*) FROM weather_forecasts WHERE location_id=?"
        " AND model='gfs_seamless' AND valid_time=?",
        (testburg_id, missing)).fetchone()[0]
    conn.close()
    assert n == 1


# ---------------------------------------------------------------------------
# ledger
# ---------------------------------------------------------------------------

def test_ledger_rows_written(adv_db):
    summary = run_advanced_collection(db_path=adv_db, max_workers=2)
    conn = sqlite3.connect(adv_db)
    main = conn.execute(
        "SELECT component, status, ended_at FROM ingestion_runs"
        " WHERE run_id=?", (summary["run_id"],)).fetchone()
    children = conn.execute(
        "SELECT COUNT(*) FROM ingestion_runs WHERE run_id LIKE ?",
        (summary["run_id"] + "/%",)).fetchone()[0]
    conn.close()
    assert main is not None
    assert main[0] == "advanced_collect"
    assert main[1] in ("ok", "partial")
    assert main[2] is not None, "ledger row must be finished (ended_at set)"
    # children: 2 locations + 8 global feeds
    assert children >= 10


# ---------------------------------------------------------------------------
# failure isolation
# ---------------------------------------------------------------------------

def test_source_failure_isolated_soft(adv_db, monkeypatch):
    add_location(adv_db, "Failtown", 30.0, 40.0)

    def fake_collect(conn, location_id, lat, lon, session=None, forecast_days=7):
        if lat == 30.0:
            raise RuntimeError("upstream exploded")
        return ok_openmeteo(conn, location_id, lat, lon)

    monkeypatch.setattr(advanced._rc.openmeteo, "collect", fake_collect)
    summary = run_advanced_collection(db_path=adv_db, max_workers=2)
    assert summary["hard_failures"] == []
    assert "Failtown:weather_forecasts" in summary["source_failures"]
    assert summary["locations"]["results"]["Failtown"]["status"] == "partial"
    assert summary["locations"]["results"]["Yangon"]["status"] == "ok"


def test_location_hard_failure_does_not_fail_run(adv_db, monkeypatch):
    add_location(adv_db, "Failtown", 30.0, 40.0)
    orig = advanced._rc.collect_location

    def flaky(conn, loc, session, primary):
        if loc["name"] == "Failtown":
            raise RuntimeError("boom")
        return orig(conn, loc, session, primary)

    monkeypatch.setattr(advanced._rc, "collect_location", flaky)
    summary = run_advanced_collection(db_path=adv_db, max_workers=2)
    assert summary["hard_failures"] == ["location:Failtown"]
    assert summary["locations"]["failed"] == ["Failtown"]
    assert summary["locations"]["results"]["Yangon"]["status"] == "ok"
    assert summary["status"] == "failed"
    # ledger still finalized despite the hard failure
    conn = sqlite3.connect(adv_db)
    row = conn.execute("SELECT status, ended_at FROM ingestion_runs WHERE run_id=?",
                       (summary["run_id"],)).fetchone()
    conn.close()
    assert row == ("failed", summary["ended_at"])
