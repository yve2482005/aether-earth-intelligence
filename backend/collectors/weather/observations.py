"""Weather observations collector.

Honesty note: Open-Meteo's `current` block is a model nowcast, NOT a station
measurement. It is stored with source='open-meteo-nowcast' and
quality='nowcast_model_estimate' so downstream consumers never mistake it for a
physical station observation. kind=OBSERVATION with documented quality.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict

from backend.common.http import fetch_json, make_session
from backend.common.logging_setup import setup_logging
from backend.common.timeutil import utc_iso
from backend.collectors.weather.openmeteo import (CURRENT_VARS, FORECAST_URL,
                                                  _record_health)
from backend.processing.normalization import normalize_openmeteo_current
from backend.processing.validation import validate_observation_row

log = setup_logging("aether.collectors.weather.observations")

INSERT_SQL = """INSERT OR IGNORE INTO weather_observations
(location_id, station, observed_at, temperature, humidity, pressure,
 wind_speed, wind_direction, precipitation, source, quality, collected_at)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"""


def collect(conn: sqlite3.Connection, location_id: int, lat: float, lon: float,
            session=None) -> Dict[str, Any]:
    session = session or make_session()
    source_id = "open-meteo-nowcast"
    res = fetch_json(session, FORECAST_URL, params={
        "latitude": lat, "longitude": lon, "current": CURRENT_VARS,
        "timezone": "UTC", "temperature_unit": "celsius",
        "wind_speed_unit": "ms", "precipitation_unit": "mm",
    })
    if not res["ok"]:
        _record_health(conn, source_id, False, res["latency_ms"], 0, res["error"])
        return {"ok": False, "error": res["error"]}
    norm = normalize_openmeteo_current(res["data"], wind_unit="ms")
    clean, errors = validate_observation_row({
        "observed_at": norm["valid_time"], "temperature": norm.get("temperature"),
        "humidity": norm.get("humidity"), "pressure": norm.get("pressure"),
        "wind_speed": norm.get("wind_speed"), "wind_direction": norm.get("wind_direction"),
        "precipitation": norm.get("precipitation"),
    })
    if "missing:observed_at" in errors:
        _record_health(conn, source_id, False, res["latency_ms"], 0, "no current time in payload")
        return {"ok": False, "error": "no current time in payload"}
    cur = conn.execute(INSERT_SQL, (
        location_id, "open-meteo-nowcast", clean["observed_at"], clean.get("temperature"),
        clean.get("humidity"), clean.get("pressure"), clean.get("wind_speed"),
        clean.get("wind_direction"), clean.get("precipitation"),
        "open-meteo-nowcast", "nowcast_model_estimate", utc_iso()))
    conn.commit()
    inserted = cur.rowcount
    _record_health(conn, source_id, True, res["latency_ms"], inserted, None)
    log.info("observations: inserted=%d dup=%d", inserted, 1 - inserted)
    return {"ok": True, "inserted": inserted, "observed_at": clean["observed_at"]}
