"""ERA5 archive backfill for observation gaps.

Open-Meteo's archive API serves ERA5 reanalysis (assimilated station +
satellite data) for past dates. Stored with source='era5-archive' and
quality='reanalysis' so it is never mistaken for a live nowcast or a
physical station reading.

Use case (2026-11-06): Tier B locations were starved of nowcast
observations by the fan-out bug (zero observations, forecasts stale
since 2026-10-18). This backfill gives them a scorable observation
history immediately; forward nowcasts remain the live source. The
scoring must label the reference per location (nowcast vs era5).
"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict

from backend.common.http import fetch_json, make_session
from backend.common.logging_setup import setup_logging
from backend.common.timeutil import utc_iso
from backend.processing.validation import validate_observation_row

log = setup_logging("aether.collectors.weather.archive_backfill")

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
ARCHIVE_VARS = ("temperature_2m,relative_humidity_2m,pressure_msl,"
                "wind_speed_10m,wind_direction_10m,precipitation")
SOURCE = "era5-archive"
QUALITY = "reanalysis"

INSERT_SQL = """INSERT OR IGNORE INTO weather_observations
(location_id, station, observed_at, temperature, humidity, pressure,
 wind_speed, wind_direction, precipitation, source, quality, collected_at)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"""


def fetch_archive(session, lat: float, lon: float,
                  start_date: str, end_date: str) -> Dict[str, Any]:
    """Fetch hourly ERA5 values for [start_date, end_date]."""
    res = fetch_json(session, ARCHIVE_URL, params={
        "latitude": lat, "longitude": lon,
        "start_date": start_date, "end_date": end_date,
        "hourly": ARCHIVE_VARS, "timezone": "UTC",
        "temperature_unit": "celsius", "wind_speed_unit": "ms",
        "precipitation_unit": "mm",
    })
    if not res["ok"]:
        return {"ok": False, "error": res["error"]}
    hourly = (res["data"] or {}).get("hourly") or {}
    if not hourly.get("time"):
        return {"ok": False, "error": "no hourly time series in response"}
    return {"ok": True, "hourly": hourly}


def _iso_z(t: str) -> str:
    """'2026-11-05T00:00' -> '2026-11-05T00:00:00Z'."""
    t = (t or "").strip()
    if len(t) == 16 and t[10] == "T":
        return t + ":00Z"
    if t.endswith("Z"):
        return t
    return t + "Z" if t else t


def store_archive(conn: sqlite3.Connection, location_id: int,
                  hourly: Dict[str, Any]) -> Dict[str, int]:
    """Store hourly ERA5 rows. Returns inserted/skipped/error counts.
    INSERT OR IGNORE on UNIQUE(location_id, source, observed_at) makes
    re-runs safe."""
    times = hourly.get("time") or []
    n = len(times)
    cols = {
        "temperature": hourly.get("temperature_2m") or [],
        "humidity": hourly.get("relative_humidity_2m") or [],
        "pressure": hourly.get("pressure_msl") or [],
        "wind_speed": hourly.get("wind_speed_10m") or [],
        "wind_direction": hourly.get("wind_direction_10m") or [],
        "precipitation": hourly.get("precipitation") or [],
    }
    inserted = skipped = errors = 0
    rows = []
    for i in range(n):
        observed_at = _iso_z(times[i])
        if not observed_at:
            errors += 1
            continue
        temp = cols["temperature"][i] if i < len(cols["temperature"]) else None
        if temp is None:
            skipped += 1
            continue
        clean, errs = validate_observation_row({
            "observed_at": observed_at,
            "temperature": temp,
            "humidity": cols["humidity"][i] if i < len(cols["humidity"]) else None,
            "pressure": cols["pressure"][i] if i < len(cols["pressure"]) else None,
            "wind_speed": cols["wind_speed"][i] if i < len(cols["wind_speed"]) else None,
            "wind_direction": cols["wind_direction"][i] if i < len(cols["wind_direction"]) else None,
            "precipitation": cols["precipitation"][i] if i < len(cols["precipitation"]) else None,
        })
        if errs:
            # Any out-of-range field: skip the whole row. (The live
            # collectors rely on the ValidatingConnection gate for this;
            # the backfill uses a plain connection, so it enforces the
            # same rule here. ERA5 essentially never trips it.)
            skipped += 1
            continue
        rows.append((location_id, SOURCE, clean["observed_at"],
                     clean.get("temperature"), clean.get("humidity"),
                     clean.get("pressure"), clean.get("wind_speed"),
                     clean.get("wind_direction"), clean.get("precipitation"),
                     SOURCE, QUALITY, utc_iso()))
    if rows:
        cur = conn.executemany(INSERT_SQL, rows)
        conn.commit()
        inserted = cur.rowcount
        skipped += len(rows) - inserted  # already-present rows
    return {"inserted": inserted, "skipped": skipped, "errors": errors}


def backfill_location(conn: sqlite3.Connection, location_id: int,
                      lat: float, lon: float, start_date: str,
                      end_date: str, session=None) -> Dict[str, Any]:
    session = session or make_session()
    res = fetch_archive(session, lat, lon, start_date, end_date)
    if not res["ok"]:
        return {"ok": False, "error": res["error"]}
    out = store_archive(conn, location_id, res["hourly"])
    log.info("archive backfill loc=%s inserted=%d skipped=%d errors=%d",
             location_id, out["inserted"], out["skipped"], out["errors"])
    return {"ok": True, **out}
