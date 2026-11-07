"""GET /v1/weather/* — forecast + observation reads from the local DB.

All values carry kind tags (FORECAST / OBSERVATION). If no collector data
exists for the location, the response is an honest 'unavailable' state, never
fabricated values.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query

from backend.api._util import confidence_payload, provenance, resolve_location, unavailable
from backend.common.timeutil import age_minutes, utc_iso
from backend.db import db as dbmod

router = APIRouter(prefix="/v1/weather", tags=["weather"])
MODEL_PRIORITY = ["gfs_seamless", "icon_seamless", "ecmwf_ifs", "jma_seamless"]

# v2: extended current-condition fields (all nullable in DB; absent -> None, never invented).
EXTENDED_KEYS = ("dew_point", "wind_gust", "visibility",
                 "pm2_5", "pm10", "nitrogen_dioxide", "ozone",
                 "sulphur_dioxide", "carbon_monoxide")


def _conn():
    c = dbmod.connect()
    try:
        yield c
    finally:
        c.close()


def _forecast_row(conn, location_id: int, model: str, valid_time: str) -> Optional[dict]:
    return dbmod.query_one(conn, """SELECT * FROM weather_forecasts
        WHERE location_id = ? AND source = 'open-meteo' AND model = ?
          AND valid_time <= ? ORDER BY valid_time DESC LIMIT 1""",
        (location_id, model, valid_time))


@router.get("/current")
def current(lat: Optional[float] = Query(None), lon: Optional[float] = Query(None),
            location_id: Optional[int] = Query(None),
            conn=Depends(_conn)) -> Dict[str, Any]:
    loc = resolve_location(conn, lat, lon, location_id)
    lid = loc["id"]
    now = utc_iso()
    fc = None
    used_model = None
    for m in MODEL_PRIORITY:
        fc = _forecast_row(conn, lid, m, now)
        if fc:
            used_model = m
            break
    obs = dbmod.query_one(conn, """SELECT * FROM weather_observations WHERE location_id = ?
        ORDER BY observed_at DESC LIMIT 1""", (lid,))
    if fc is None and obs is None:
        return unavailable(
            "No weather data for this location yet. Run the Open-Meteo collector first.",
            ["open-meteo"], None, kind="FORECAST")
    data: Dict[str, Any] = {}
    prov_sources = []
    if fc:
        prov_sources.append(f"open-meteo/{used_model}")
        spread = None
        vals = [r["temperature"] for r in dbmod.query(
            conn, """SELECT temperature FROM weather_forecasts WHERE location_id = ?
               AND valid_time = (SELECT MAX(valid_time) FROM weather_forecasts
                                 WHERE location_id = ? AND valid_time <= ?)""",
            (lid, lid, now)) if r["temperature"] is not None]
        if len(vals) >= 2:
            spread = max(vals) - min(vals)
        conf = confidence_payload("FORECAST", data_age_min=age_minutes(fc["collected_at"]),
                                  model_spread=spread, model_count=len(vals), horizon_hours=0)
        data["forecast_now"] = {**{k: fc[k] for k in (
            "temperature", "humidity", "pressure", "wind_speed", "wind_direction",
            "precipitation", "precip_probability", "cloud_cover", "uv", "aqi",
            *EXTENDED_KEYS)},
            "valid_time": fc["valid_time"], "model": used_model,
            "kind": "FORECAST", "confidence": conf}
    if obs:
        prov_sources.append("open-meteo-nowcast")
        data["observation"] = {**{k: obs[k] for k in (
            "temperature", "humidity", "pressure", "wind_speed", "wind_direction",
            "precipitation")}, "observed_at": obs["observed_at"],
            "quality": obs["quality"], "kind": "OBSERVATION"}
    updated = max([x for x in [fc["collected_at"] if fc else None,
                               obs["collected_at"] if obs else None] if x], default=None)
    return {"location": {"id": lid, "name": loc["name"], "latitude": loc["latitude"],
                         "longitude": loc["longitude"]},
            **data, "provenance": provenance(prov_sources, updated)}


@router.get("/hourly")
def hourly(lat: Optional[float] = Query(None), lon: Optional[float] = Query(None),
           location_id: Optional[int] = Query(None),
           model: str = Query("gfs_seamless"), hours: int = Query(48, le=168),
           conn=Depends(_conn)) -> Dict[str, Any]:
    loc = resolve_location(conn, lat, lon, location_id)
    rows = dbmod.query(conn, """SELECT * FROM weather_forecasts
        WHERE location_id = ? AND source = 'open-meteo' AND model = ?
          AND valid_time >= strftime('%Y-%m-%dT%H:%M:%SZ','now', '-2 hours')
        ORDER BY valid_time LIMIT ?""", (loc["id"], model, hours))
    if not rows:
        return unavailable(f"No hourly forecast rows for model {model} at this location.",
                           ["open-meteo"], None, kind="FORECAST")
    items = [{**{k: r[k] for k in ("temperature", "humidity", "pressure", "wind_speed",
                                   "wind_direction", "precipitation", "precip_probability",
                                   "cloud_cover", "uv", "aqi", *EXTENDED_KEYS)},
              "valid_time": r["valid_time"], "forecast_hour": r["forecast_hour"],
              "kind": "FORECAST"} for r in rows]
    updated = max(r["collected_at"] for r in rows)
    return {"location_id": loc["id"], "model": model, "count": len(items), "items": items,
            "provenance": provenance([f"open-meteo/{model}"], updated)}


@router.get("/daily")
def daily(lat: Optional[float] = Query(None), lon: Optional[float] = Query(None),
          location_id: Optional[int] = Query(None),
          model: str = Query("gfs_seamless"), days: int = Query(7, le=16),
          conn=Depends(_conn)) -> Dict[str, Any]:
    loc = resolve_location(conn, lat, lon, location_id)
    rows = dbmod.query(conn, """SELECT date(valid_time) AS day,
            MIN(temperature) AS tmin, MAX(temperature) AS tmax,
            SUM(COALESCE(precipitation,0)) AS precip_sum,
            MAX(precip_probability) AS precip_prob_max,
            MAX(wind_speed) AS wind_max, MAX(uv) AS uv_max,
            MAX(collected_at) AS collected_at
        FROM weather_forecasts
        WHERE location_id = ? AND source = 'open-meteo' AND model = ?
          AND valid_time >= strftime('%Y-%m-%dT%H:%M:%SZ','now', '-2 hours')
        GROUP BY day ORDER BY day LIMIT ?""", (loc["id"], model, days))
    if not rows:
        return unavailable(f"No daily forecast rows for model {model} at this location.",
                           ["open-meteo"], None, kind="FORECAST")
    items = [{**{k: r[k] for k in ("day", "tmin", "tmax", "precip_sum",
                                   "precip_prob_max", "wind_max", "uv_max")},
              "kind": "FORECAST"} for r in rows]
    return {"location_id": loc["id"], "model": model, "count": len(items), "items": items,
            "provenance": provenance([f"open-meteo/{model}"], max(r["collected_at"] for r in rows))}


@router.get("/map")
def weather_map(conn=Depends(_conn)) -> Dict[str, Any]:
    row = dbmod.query_one(conn, """SELECT checked_at FROM system_health
        WHERE component IN ('collector:satellite','collector:radar')
        ORDER BY checked_at DESC LIMIT 1""")
    return unavailable(
        "No satellite/radar tile source is wired yet. Map imagery is honestly unavailable.",
        ["satellite", "radar"], row["checked_at"] if row else None, kind="OBSERVATION")
