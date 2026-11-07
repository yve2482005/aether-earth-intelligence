"""Nowcast engine tests (workstream B, spec §§1, 24).

Verifies NOWCAST vs NWP FORECAST labeling and honest degradation:
fresh observation -> NOWCAST-anchored windows; stale/absent
observation -> 'nowcast unavailable - showing NWP forecast'; no NWP
background at all -> status 'unavailable'.
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.engines import nowcast  # noqa: E402


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _nwp(db_path: str, base: datetime, model: str = "gfs_seamless"):
    conn = sqlite3.connect(db_path)
    for h in range(-1, 4):
        vt = _iso(base + timedelta(hours=h))
        conn.execute(
            """INSERT INTO weather_forecasts
               (location_id, source, model, forecast_run, forecast_hour,
                issued_at, valid_time, temperature, humidity, pressure,
                wind_speed, precipitation, precip_probability, cloud_cover,
                collected_at)
               VALUES (1,'open-meteo',?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (model, vt, h, vt, vt, 30.0 - 0.05 * h, 70.0, 1008.0,
             3.0, 0.0, 0.1, 20.0, vt))
    conn.commit()
    conn.close()


def _obs(db_path: str, observed_at: datetime, temp: float = 29.5):
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO weather_observations
           (location_id, station, observed_at, temperature, humidity,
            pressure, wind_speed, source, quality, collected_at)
           VALUES (1,'TEST-ST',?,?,?,?,?,?, 'ok', ?)""",
        (_iso(observed_at), temp, 75.0, 1007.0, 2.5, "synop",
         _iso(observed_at)))
    conn.commit()
    conn.close()


def _conn(db_path: str) -> sqlite3.Connection:
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    return c


def test_anchored_nowcast_labels_all_windows_nowcast(weather_db):
    base = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    _nwp(weather_db, base)
    # Obs must be fresh relative to REAL now (the engine ages it against
    # now_utc(), not the hour floor): base-10min is 10-69 min old
    # depending on the minute the test runs, and past minute 51 the age
    # exceeds MAX_OBS_AGE_MIN=60 — the old fixture flaked then.
    _obs(weather_db, datetime.now(timezone.utc) - timedelta(minutes=10))
    out = nowcast.build(_conn(weather_db), 1)
    assert out["status"] == "ok"
    assert out["kind"] == "NOWCAST"
    assert out["anchored"] is True
    assert len(out["windows"]) == 4
    kinds = [w["kind"] for w in out["windows"]]
    assert all(k == "NOWCAST" for k in kinds), kinds
    # observation weight decays with lead time: 0-15min heaviest, 60-120 lightest
    ws = [w["observation_weight"] for w in out["windows"]]
    assert ws == sorted(ws, reverse=True)
    assert ws[0] > ws[-1] >= 0.0
    assert out["observation"]["kind"] == "OBSERVATION"
    assert out["provenance"]["sources"] == ["observation", "open-meteo"]


def test_stale_observation_degrades_to_nwp_forecast(weather_db):
    base = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    _nwp(weather_db, base)
    _obs(weather_db, base - timedelta(minutes=120))  # older than 60min max
    out = nowcast.build(_conn(weather_db), 1)
    assert out["status"] == "nowcast_unavailable"
    assert out["kind"] == "FORECAST"
    assert out["anchored"] is False
    assert len(out["windows"]) == 4
    assert all(w["kind"] == "NWP FORECAST" for w in out["windows"])
    assert "nowcast unavailable" in out["message"].lower()
    assert out["observation"]["note"] == "too old to anchor a nowcast"


def test_no_observation_degrades_to_nwp_forecast(weather_db):
    base = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    _nwp(weather_db, base)
    out = nowcast.build(_conn(weather_db), 1)
    assert out["status"] == "nowcast_unavailable"
    assert all(w["kind"] == "NWP FORECAST" for w in out["windows"])
    assert "NWP" in out["message"]


def test_no_nwp_and_no_obs_unavailable(weather_db):
    out = nowcast.build(_conn(weather_db), 1)
    assert out["status"] == "unavailable"
    assert out["windows"] == []
    assert "neither" in out["message"].lower()
