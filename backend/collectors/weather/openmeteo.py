"""Open-Meteo weather collector (multi-model).

Fetches, for one location, per-NWP-model:
  current + hourly (temperature_2m, relative_humidity_2m, pressure_msl, wind_speed_10m,
  wind_direction_10m, precipitation, precipitation_probability, cloud_cover, weather_code,
  uv_index) + daily, plus CAMS air-quality (US AQI).

Honesty notes:
- `forecast_run` is recorded as the collection time truncated to the hour; the
  Open-Meteo free API does not expose the upstream model run timestamp.
- One model failing never crashes the others; per-model health is recorded.
- All models are fetched in ONE combined API call when possible (verified
  live 2026-10-17: the endpoint returns a single object whose hourly/daily
  variables are suffixed `<var>_<model>` over a shared time axis; `current`
  is one unsuffixed blend and is not used per-model). The split payloads are
  byte-equivalent in shape to single-model responses. If the combined call
  fails or its shape is not recognised, each model falls back to its own
  fetch_model() call, so collection never depends on the combined path.
- Duplicates are ignored via the UNIQUE(location_id, source, model, forecast_run, forecast_hour)
  constraint; stale responses (all valid times older than 6h) mark the source degraded.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional

from backend.common.config import HTTP_TIMEOUT
from backend.common.http import fetch_json, make_session
from backend.common.logging_setup import setup_logging
from backend.common.timeutil import (age_minutes, hour_floor, now_utc, parse_iso, utc_iso)
from backend.processing.normalization import (normalize_openmeteo_daily,
                                              normalize_openmeteo_hourly)
from backend.processing.validation import validate_forecast_row

log = setup_logging("aether.collectors.weather.openmeteo")

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
AIR_QUALITY_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"

MODELS = ["gfs_seamless", "icon_seamless", "ecmwf_ifs", "jma_seamless"]

HOURLY_VARS = ",".join([
    "temperature_2m", "relative_humidity_2m", "pressure_msl", "wind_speed_10m",
    "wind_direction_10m", "wind_gusts_10m", "precipitation", "precipitation_probability",
    "cloud_cover", "weather_code", "uv_index", "visibility", "dew_point_2m",
])
DAILY_VARS = ",".join([
    "temperature_2m_max", "temperature_2m_min", "precipitation_sum",
    "precipitation_probability_max", "wind_speed_10m_max", "uv_index_max", "weather_code",
    "sunrise", "sunset",
])
# Pollutants from the Open-Meteo air-quality API (CAMS). Units: ug/m3.
POLLUTANT_VARS = ",".join([
    "pm2_5", "pm10", "nitrogen_dioxide", "ozone", "sulphur_dioxide", "carbon_monoxide",
])
POLLUTANT_COLS = ["pm2_5", "pm10", "nitrogen_dioxide", "ozone", "sulphur_dioxide",
                  "carbon_monoxide"]
CURRENT_VARS = ",".join([
    "temperature_2m", "relative_humidity_2m", "apparent_temperature", "pressure_msl",
    "wind_speed_10m", "wind_direction_10m", "precipitation", "weather_code", "cloud_cover",
])

INSERT_SQL = """INSERT OR IGNORE INTO weather_forecasts
(location_id, source, model, forecast_run, forecast_hour, issued_at, valid_time,
 temperature, feels_like, humidity, pressure, wind_speed, wind_direction, precipitation,
 precip_probability, cloud_cover, visibility, uv, aqi, collected_at,
 dew_point, wind_gust, pm2_5, pm10, nitrogen_dioxide, ozone, sulphur_dioxide, carbon_monoxide)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
        ?,?,?,?,?,?,?,?)"""


def _record_health(conn: sqlite3.Connection, source_id: str, ok: bool,
                   latency_ms: Optional[float], records: int, message: Optional[str]) -> None:
    conn.execute(
        """INSERT INTO system_health (component, status, last_success, last_failure,
                                      latency_ms, records, message, checked_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (f"collector:{source_id}", "ok" if ok else "down",
         utc_iso() if ok else None, None if ok else utc_iso(),
         latency_ms, records, message, utc_iso()))
    conn.execute(
        """UPDATE source_registry SET last_success = CASE WHEN ? THEN ? ELSE last_success END,
                                      last_failure = CASE WHEN ? THEN last_failure ELSE ? END,
                                      health = ?
           WHERE source_id = ?""",
        (1 if ok else 0, utc_iso(), 1 if ok else 0, utc_iso(),
         "ok" if ok else "down", source_id))
    conn.commit()


def fetch_model(lat: float, lon: float, model: str, session,
                forecast_days: int = 7) -> Dict[str, Any]:
    params = {
        "latitude": lat, "longitude": lon,
        "models": model,
        "hourly": HOURLY_VARS, "daily": DAILY_VARS, "current": CURRENT_VARS,
        "timezone": "UTC", "forecast_days": forecast_days,
        "temperature_unit": "celsius", "wind_speed_unit": "ms", "precipitation_unit": "mm",
    }
    return fetch_json(session, FORECAST_URL, params=params, timeout=HTTP_TIMEOUT)


def fetch_models_combined(lat: float, lon: float, models: List[str], session,
                          forecast_days: int = 7) -> Dict[str, Any]:
    """Fetch ALL models for one location in a single API call."""
    params = {
        "latitude": lat, "longitude": lon,
        "models": ",".join(models),
        "hourly": HOURLY_VARS, "daily": DAILY_VARS, "current": CURRENT_VARS,
        "timezone": "UTC", "forecast_days": forecast_days,
        "temperature_unit": "celsius", "wind_speed_unit": "ms", "precipitation_unit": "mm",
    }
    return fetch_json(session, FORECAST_URL, params=params, timeout=HTTP_TIMEOUT)


def split_combined_payload(data: Any, models: List[str]) -> Dict[str, Dict[str, Any]]:
    """Split a combined multi-model response into {model: single-model payload}.

    Verified live shape (2026-10-17): one dict; `hourly`/`daily` hold a shared
    "time" axis plus one `<var>_<model>` series per requested variable/model.
    A JSON array with one payload object per model (request order) is also
    accepted defensively. Returns {} when the shape is not recognised, so the
    caller falls back to per-model fetches.
    """
    if isinstance(data, list):
        if (len(data) == len(models)
                and all(isinstance(el, dict) and "hourly" in el for el in data)):
            return {m: el for m, el in zip(models, data)}
        return {}
    if not isinstance(data, dict):
        return {}
    hourly = data.get("hourly") or {}
    daily = data.get("daily") or {}
    if not hourly.get("time"):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for model in models:
        h: Dict[str, Any] = {"time": hourly["time"]}
        found = False
        for var in HOURLY_VARS.split(","):
            key = f"{var}_{model}"
            if key in hourly:
                h[var] = hourly[key]
                found = True
        if not found:
            return {}  # unexpected shape -> caller falls back per-model
        d: Dict[str, Any] = {}
        if daily.get("time"):
            d["time"] = daily["time"]
            for var in DAILY_VARS.split(","):
                key = f"{var}_{model}"
                if key in daily:
                    d[var] = daily[key]
        out[model] = {"hourly": h, "daily": d}
    return out


def store_model_payload(conn: sqlite3.Connection, location_id: int, model: str,
                        payload: Dict[str, Any]) -> Dict[str, int]:
    """Normalize + validate + insert one model's payload. Returns counts."""
    run = utc_iso(hour_floor())
    collected = utc_iso()
    rows = normalize_openmeteo_hourly(payload, model, wind_unit="ms")
    run_dt = parse_iso(run)
    invalid = 0
    params = []
    for r in rows:
        valid_dt = parse_iso(r["valid_time"])
        forecast_hour = int(round((valid_dt - run_dt).total_seconds() / 3600.0))
        clean, errors = validate_forecast_row({
            "valid_time": r["valid_time"], "temperature": r.get("temperature"),
            "humidity": r.get("humidity"), "pressure": r.get("pressure"),
            "wind_speed": r.get("wind_speed"), "wind_direction": r.get("wind_direction"),
            "precipitation": r.get("precipitation"),
            "precip_probability": r.get("precip_probability"),
            "cloud_cover": r.get("cloud_cover"), "uv": r.get("uv"),
            "visibility": r.get("visibility"), "dew_point": r.get("dew_point"),
            "wind_gust": r.get("wind_gust"),
        })
        if "missing:valid_time" in errors:
            invalid += 1
            continue
        params.append((
            location_id, "open-meteo", model, run, forecast_hour, run, clean["valid_time"],
            clean.get("temperature"), None, clean.get("humidity"), clean.get("pressure"),
            clean.get("wind_speed"), clean.get("wind_direction"), clean.get("precipitation"),
            clean.get("precip_probability"), clean.get("cloud_cover"),
            clean.get("visibility"),
            clean.get("uv"), None, collected,
            clean.get("dew_point"), clean.get("wind_gust"),
            None, None, None, None, None, None))  # pollutants attached separately
    # executemany (changed 2026-10-18): the old per-row execute loop held
    # the SQLite write lock for the whole ~168-row transaction; under disk
    # load that exceeded other writers' busy_timeout and produced the
    # recurring "database is locked" storms. One executemany keeps the
    # same gate + INSERT OR IGNORE semantics (rowcount = rows actually
    # inserted) with a far shorter lock hold.
    inserted = 0
    if params:
        cur = conn.executemany(INSERT_SQL, params)
        inserted = cur.rowcount if cur.rowcount is not None else 0
    dupes = len(params) - inserted
    conn.commit()
    return {"inserted": inserted, "duplicates": dupes, "invalid": invalid, "total": len(rows)}


def attach_air_quality(conn: sqlite3.Connection, location_id: int, lat: float, lon: float,
                       session) -> Dict[str, Any]:
    """Attach CAMS US-AQI + pollutant concentrations to stored forecast rows (any model)."""
    res = fetch_json(session, AIR_QUALITY_URL, params={
        "latitude": lat, "longitude": lon, "hourly": "us_aqi," + POLLUTANT_VARS,
        "timezone": "UTC", "forecast_days": 7,
    }, timeout=HTTP_TIMEOUT)
    if not res["ok"]:
        _record_health(conn, "open-meteo-air-quality", False, res["latency_ms"], 0, res["error"])
        return {"ok": False, "error": res["error"]}
    hourly = (res["data"] or {}).get("hourly") or {}
    times = hourly.get("time") or []
    series = {var: (hourly.get(var) or []) for var in ["us_aqi", *POLLUTANT_VARS.split(",")]}
    updates = []
    for i, t in enumerate(times):
        vals = {var: (s[i] if i < len(s) else None) for var, s in series.items()}
        if all(v is None for v in vals.values()):
            continue
        updates.append(
            (vals["us_aqi"], vals["pm2_5"], vals["pm10"], vals["nitrogen_dioxide"],
             vals["ozone"], vals["sulphur_dioxide"], vals["carbon_monoxide"],
             location_id, utc_iso(parse_iso(t))))
    updated = 0
    if updates:
        cur = conn.executemany(
            """UPDATE weather_forecasts
               SET aqi = COALESCE(aqi, ?),
                   pm2_5 = COALESCE(pm2_5, ?), pm10 = COALESCE(pm10, ?),
                   nitrogen_dioxide = COALESCE(nitrogen_dioxide, ?),
                   ozone = COALESCE(ozone, ?),
                   sulphur_dioxide = COALESCE(sulphur_dioxide, ?),
                   carbon_monoxide = COALESCE(carbon_monoxide, ?)
               WHERE location_id = ? AND source = 'open-meteo' AND valid_time = ?""",
            updates)
        updated = cur.rowcount if cur.rowcount is not None else 0
    conn.commit()
    _record_health(conn, "open-meteo-air-quality", True, res["latency_ms"], updated, None)
    return {"ok": True, "updated": updated}


def store_daily_payload(conn: sqlite3.Connection, location_id: int, model: str,
                        payload: Dict[str, Any]) -> Dict[str, int]:
    """Store daily rows (currently: sunrise/sunset) — additive, never blocks models."""
    daily = (payload.get("daily") or {})
    times = daily.get("time") or []
    sunrises = daily.get("sunrise") or []
    sunsets = daily.get("sunset") or []
    collected = utc_iso()
    params = []
    for i, t in enumerate(times):
        sr = sunrises[i] if i < len(sunrises) else None
        ss = sunsets[i] if i < len(sunsets) else None
        params.append(
            (location_id, "open-meteo", model, str(t),
             utc_iso(parse_iso(sr)) if sr else None,
             utc_iso(parse_iso(ss)) if ss else None, collected))
    inserted = 0
    if params:
        cur = conn.executemany(
            """INSERT OR IGNORE INTO weather_daily
               (location_id, source, model, date, sunrise, sunset, collected_at)
               VALUES (?,?,?,?,?,?,?)""",
            params)
        inserted = cur.rowcount if cur.rowcount is not None else 0
    conn.commit()
    return {"inserted": inserted, "total": len(times)}


def _process_model_payload(conn: sqlite3.Connection, location_id: int, model: str,
                             payload: Dict[str, Any],
                             latency_ms: Optional[float]) -> Dict[str, Any]:
    """Stale-check + store one model's payload; record per-model health.

    Behaviour is identical whether the payload came from a single-model
    fetch or from splitting a combined multi-model response.
    """
    source_id = f"open-meteo-{model}"
    try:
        # Stale detection: all valid times older than 6h -> degraded.
        times = ((payload.get("hourly") or {}).get("time")) or []
        newest = max((parse_iso(t) for t in times), default=None)
        if newest is not None and age_minutes(utc_iso(newest)) > 360:
            _record_health(conn, source_id, False, latency_ms, 0,
                           "stale payload: newest valid_time older than 6h")
            return {"ok": False, "error": "stale payload"}
        counts = store_model_payload(conn, location_id, model, payload)
        try:
            counts["daily"] = store_daily_payload(conn, location_id, model, payload)
        except Exception as e:  # daily rows are a bonus; never fail the model
            log.warning("daily store failed for %s: %s", model, e)
        _record_health(conn, source_id, True, latency_ms, counts["inserted"], None)
        log.info("open-meteo %s: %s", model, counts)
        return {"ok": True, **counts,
                "latency_ms": round(latency_ms, 1) if latency_ms is not None else None}
    except Exception as e:  # never let one model kill the run
        log.exception("model %s failed", model)
        _record_health(conn, source_id, False, None, 0, f"{type(e).__name__}: {e}")
        return {"ok": False, "error": str(e)}


def _collect_model_single(conn: sqlite3.Connection, location_id: int, model: str,
                          lat: float, lon: float, session,
                          forecast_days: int) -> Dict[str, Any]:
    """Legacy per-model path: one fetch_model() call for one model."""
    source_id = f"open-meteo-{model}"
    try:
        res = fetch_model(lat, lon, model, session, forecast_days=forecast_days)
    except Exception as e:  # never let one model kill the run
        log.exception("model %s fetch failed", model)
        _record_health(conn, source_id, False, None, 0, f"{type(e).__name__}: {e}")
        return {"ok": False, "error": str(e)}
    if not res["ok"]:
        _record_health(conn, source_id, False, res["latency_ms"], 0, res["error"])
        return {"ok": False, "error": res["error"]}
    return _process_model_payload(conn, location_id, model, res["data"],
                                  res["latency_ms"])


def collect(conn: sqlite3.Connection, location_id: int, lat: float, lon: float,
            models: Optional[List[str]] = None, session=None,
            forecast_days: int = 7) -> Dict[str, Any]:
    """Collect multi-model weather for one location. One model failure never crashes others.

    All models are requested in ONE combined API call and the response is
    split per model; any model that cannot be served from the combined
    response falls back to its own per-model fetch.
    """
    session = session or make_session()
    models = models or MODELS
    try:
        from backend.db.migrate import ensure_v2
        ensure_v2(conn)  # idempotent; keeps long-lived DBs on the v2 schema
    except Exception:
        pass
    summary: Dict[str, Any] = {"location_id": location_id, "models": {}, "air_quality": None}
    payloads: Dict[str, Dict[str, Any]] = {}
    combined_latency: Optional[float] = None
    if len(models) > 1:
        try:
            res = fetch_models_combined(lat, lon, models, session,
                                        forecast_days=forecast_days)
            if res["ok"]:
                payloads = split_combined_payload(res["data"], models)
                if payloads:
                    combined_latency = res["latency_ms"]
                else:
                    log.warning("combined payload shape not recognised for "
                                "location %s; falling back to per-model fetch",
                                location_id)
            else:
                log.warning("combined fetch failed for location %s (%s); "
                            "falling back to per-model fetch",
                            location_id, res["error"])
        except Exception:
            log.exception("combined fetch raised for location %s; "
                          "falling back to per-model fetch", location_id)
            payloads = {}
    for model in models:
        if model in payloads:
            summary["models"][model] = _process_model_payload(
                conn, location_id, model, payloads[model], combined_latency)
        else:
            summary["models"][model] = _collect_model_single(
                conn, location_id, model, lat, lon, session, forecast_days)
    try:
        summary["air_quality"] = attach_air_quality(conn, location_id, lat, lon, session)
    except Exception as e:
        log.exception("air quality failed")
        summary["air_quality"] = {"ok": False, "error": str(e)}
    return summary
