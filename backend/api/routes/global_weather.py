"""GET /v1/global/* — the Global Weather System layer.

One unified answer for any place on Earth, on top of the collected data
engine (worldwide collectors, multi-model forecasts, observations, ML
predictions, alerts):

  GET /v1/global/search?q=<name>   resolve a place name. Collected
      locations are matched first (name/country, case-insensitive);
      Open-Meteo geocoding candidates are merged in so ANY real city
      name resolves — collected coverage is a subset of world coverage,
      never the limit of it.
  GET /v1/global/weather?lat&lon   the unified weather answer for any
      coordinates. Near a collected location (<= ~25 km) the answer is
      served from the database (source="collected"); anywhere else it
      is live-fetched from Open-Meteo (source="live_fetch") and NOT
      stored — live points never become silent locations.
  GET /v1/global/overview          system summary: collected locations,
      per-source collection status, active alerts, prediction coverage,
      and the coverage statement (collected vs on-demand worldwide).
  GET /v1/global/dashboard        self-contained server-rendered HTML.

Honesty rules (project-wide): forecast, observation, ML prediction and
alert are always labelled as distinct kinds with timestamps and
staleness; ML predictions carry their model version and are never
presented as observations; missing pieces are null/absent, never
invented. All endpoints are read-only.
"""
from __future__ import annotations

import html
import math
from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse

from backend.api._util import provenance
from backend.common.http import fetch_json, make_session
from backend.common.timeutil import age_minutes, now_utc, parse_iso, utc_iso
from backend.db import db as dbmod

router = APIRouter(prefix="/v1/global", tags=["global"])

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
NEAR_KM = 25.0  # a query point within this radius is served from the DB

_LOCATION_COLS = "id, name, country, latitude, longitude, timezone, is_coastal"
_FORECAST_COLS = ("model, forecast_run, valid_time, temperature, humidity, "
                  "pressure, wind_speed, wind_direction, precipitation, "
                  "precip_probability, cloud_cover")


def _conn():
    c = dbmod.connect()
    try:
        yield c
    finally:
        c.close()


def _now() -> str:
    return utc_iso()


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# ---------------- geocoding + live fetch (module-level for testability) ----

def _geocode(name: str) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Open-Meteo geocoding (free, no key). Returns (candidates, error)."""
    session = make_session()
    res = fetch_json(session, GEOCODING_URL, params={
        "name": name, "count": 5, "language": "en", "format": "json"})
    if not res.get("ok"):
        return [], str(res.get("error") or "geocoding request failed")
    out = []
    for r in (res.get("data") or {}).get("results") or []:
        out.append({
            "kind": "geocoding_candidate",
            "collected": False,
            "location_id": None,
            "name": r.get("name"),
            "country": r.get("country"),
            "admin1": r.get("admin1"),
            "latitude": r.get("latitude"),
            "longitude": r.get("longitude"),
            "timezone": r.get("timezone"),
        })
    return out, None


def _live_fetch(lat: float, lon: float) -> Dict[str, Any]:
    """Live multi-model forecast for an arbitrary point (read-only).

    Reuses the collector's own helpers (combined fetch + split, per-model
    fallback, canonical normalization) so a live answer has exactly the
    same shape and units as a collected one. Nothing is written anywhere.
    Returns {"models": {model: [rows]}, "current": row|None,
    "fetched_at": iso}. Raises RuntimeError when nothing could be fetched.
    """
    from backend.collectors.weather.openmeteo import (
        MODELS, fetch_model, fetch_models_combined, split_combined_payload)
    from backend.processing.normalization import (
        normalize_openmeteo_current, normalize_openmeteo_hourly)

    session = make_session()
    payloads: Dict[str, Dict[str, Any]] = {}
    current = None
    if len(MODELS) > 1:
        try:
            res = fetch_models_combined(lat, lon, list(MODELS), session,
                                        forecast_days=7)
            if res.get("ok") and isinstance(res.get("data"), dict):
                if res["data"].get("current"):
                    try:
                        current = normalize_openmeteo_current(res["data"])
                    except Exception:
                        current = None
                payloads = split_combined_payload(res["data"], list(MODELS))
        except Exception:
            payloads = {}
    for model in MODELS:
        if model in payloads:
            continue
        try:
            res = fetch_model(lat, lon, model, session, forecast_days=7)
            if res.get("ok") and isinstance(res.get("data"), dict):
                payloads[model] = res["data"]
                if current is None and res["data"].get("current"):
                    try:
                        current = normalize_openmeteo_current(res["data"])
                    except Exception:
                        pass
        except Exception:
            continue
    model_rows = {}
    for model, payload in payloads.items():
        try:
            model_rows[model] = normalize_openmeteo_hourly(
                payload, model, wind_unit="ms")
        except Exception:
            continue
    if not model_rows:
        raise RuntimeError("live fetch failed for all models")
    return {"models": model_rows, "current": current, "fetched_at": _now()}


# ---------------- shared forecast shaping ---------------------------------

_HOUR_FIELDS = ("temperature", "humidity", "pressure", "wind_speed",
                "precipitation", "precip_probability")


def _shape_forecast(model_rows: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    """Daily summaries (7 days) + hourly detail (next 48 h) from canonical
    per-model rows ({valid_time, temperature, ...}). Shared by the DB path
    and the live-fetch path so both answers have the identical shape."""
    now = now_utc()
    window_start = now.replace(minute=0, second=0, microsecond=0)
    horizon = window_start + timedelta(hours=48)
    today = now.strftime("%Y-%m-%d")

    hourly_by_time: Dict[str, Dict[str, Dict[str, Any]]] = {}
    daily: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for model, rows in model_rows.items():
        for r in rows:
            vt = r.get("valid_time")
            if not vt:
                continue
            dt = parse_iso(vt)
            if dt is None:
                continue
            if window_start <= dt < horizon:
                slot = hourly_by_time.setdefault(vt, {})
                slot[model] = {f: r.get(f) for f in _HOUR_FIELDS}
            date = vt[:10]
            if date >= today:
                d = daily.setdefault(date, {}).setdefault(model, {
                    "temp_min": None, "temp_max": None, "precip_total_mm": 0.0,
                    "precip_prob_max": None, "wind_speed_max": None,
                    "_hours": 0})
                t = r.get("temperature")
                if t is not None:
                    d["temp_min"] = t if d["temp_min"] is None else min(d["temp_min"], t)
                    d["temp_max"] = t if d["temp_max"] is None else max(d["temp_max"], t)
                if r.get("precipitation") is not None:
                    d["precip_total_mm"] += r["precipitation"]
                pp = r.get("precip_probability")
                if pp is not None:
                    d["precip_prob_max"] = pp if d["precip_prob_max"] is None \
                        else max(d["precip_prob_max"], pp)
                w = r.get("wind_speed")
                if w is not None:
                    d["wind_speed_max"] = w if d["wind_speed_max"] is None \
                        else max(d["wind_speed_max"], w)
                d["_hours"] += 1

    hourly = []
    for vt in sorted(hourly_by_time):
        models = hourly_by_time[vt]
        temps = [m["temperature"] for m in models.values()
                 if m.get("temperature") is not None]
        hourly.append({
            "valid_time": vt,
            "models": models,
            "ensemble_temperature": round(sum(temps) / len(temps), 2) if temps else None,
        })

    days = []
    for date in sorted(daily)[:7]:
        per_model = {}
        mins, maxs = [], []
        for model, d in sorted(daily[date].items()):
            entry = {k: (round(v, 2) if isinstance(v, float) else v)
                     for k, v in d.items() if k != "_hours"}
            per_model[model] = entry
            if entry.get("temp_min") is not None:
                mins.append(entry["temp_min"])
            if entry.get("temp_max") is not None:
                maxs.append(entry["temp_max"])
        days.append({
            "date": date,
            "models": per_model,
            "multi_model_temp_range": {
                "min": min(mins) if mins else None,
                "max": max(maxs) if maxs else None},
        })

    return {"kind": "forecast", "models": sorted(model_rows),
            "daily": days, "hourly_next_48h": hourly}


def _active_alerts(conn, now_s: str,
                   location_id: Optional[int] = None) -> List[Dict[str, Any]]:
    sql = ("SELECT alert_id, location_id, type, severity, title, message, "
           "source, kind, issued_at, expires_at FROM alerts "
           "WHERE status = 'active' AND (expires_at IS NULL OR expires_at > ?)")
    params: List[Any] = [now_s]
    if location_id is not None:
        sql += " AND location_id = ?"
        params.append(location_id)
    sql += " ORDER BY issued_at DESC"
    out = []
    for r in conn.execute(sql, params).fetchall():
        d = dict(r)
        # the alerts table's own `kind` column (observed/forecast/...) is
        # preserved as alert_kind; the block kind stays "alert"
        d["alert_kind"] = d.pop("kind", None)
        out.append({"kind": "alert", **d})
    return out


def _predictions_block(conn, location_id: int, now_s: str) -> Optional[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT task, model_version, valid_time, prediction, predicted_at "
        "FROM ml_batch_predictions WHERE location_id = ? AND valid_time >= ? "
        "ORDER BY valid_time", (location_id, now_s)).fetchall()
    if not rows:
        return None
    best: Dict[Tuple[str, str], Dict[str, Any]] = {}
    versions = set()
    generated = None
    for r in rows:
        d = dict(r)
        versions.add(d["model_version"])
        if generated is None or (d["predicted_at"] or "") > generated:
            generated = d["predicted_at"]
        key = (d["task"], d["valid_time"])
        if key not in best or (d["predicted_at"] or "") >= (best[key]["predicted_at"] or ""):
            best[key] = d
    tasks: Dict[str, List[Dict[str, Any]]] = {}
    for (task, _vt), d in sorted(best.items()):
        tasks.setdefault(task, []).append(
            {"valid_time": d["valid_time"], "prediction": d["prediction"]})
    for task in tasks:
        tasks[task] = tasks[task][:168]
    return {
        "kind": "ml_prediction",
        "label": "ML model predictions — estimates produced by the trained "
                 "weather models; not observations and not NWP forecasts.",
        "model_versions": sorted(versions),
        "generated_at": generated,
        "tasks": tasks,
    }


def _nearest_location(conn, lat: float, lon: float):
    """Nearest collected location within NEAR_KM, preferring locations
    that actually hold forecast data (a bare locations row with no data
    must not shadow a real collected neighbour)."""
    locs = conn.execute(f"SELECT {_LOCATION_COLS} FROM locations").fetchall()
    near = []
    for r in locs:
        d = dict(r)
        dist = _haversine_km(lat, lon, d["latitude"], d["longitude"])
        if dist <= NEAR_KM:
            near.append((dist, d))
    if not near:
        return None, None
    near.sort(key=lambda x: x[0])
    for dist, d in near:
        has = conn.execute(
            "SELECT 1 FROM weather_forecasts WHERE location_id = ? LIMIT 1",
            (d["id"],)).fetchone()
        if has:
            return d, round(dist, 2)
    dist, d = near[0]
    return d, round(dist, 2)


# ---------------- search ---------------------------------------------------

def _global_places_table(conn) -> Optional[Dict[str, str]]:
    """Feature-detect the GeoNames registry table (built by another
    workstream; search must work before/without it). Returns a column
    mapping {name, country, lat, lon, population} or None."""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'global_places'").fetchone()
    if not exists:
        return None
    cols = {r[1] for r in conn.execute("PRAGMA table_info(global_places)")}
    def pick(*names):
        for n in names:
            if n in cols:
                return n
        return None
    mapping = {"name": pick("name"), "country": pick("country", "country_name",
                                                     "country_code"),
               "lat": pick("latitude", "lat"), "lon": pick("longitude", "lon", "lng"),
               "population": pick("population", "pop")}
    if not mapping["name"] or not mapping["lat"] or not mapping["lon"]:
        return None
    return mapping


def _registry_matches(conn, q: str, limit: int) -> List[Dict[str, Any]]:
    mapping = _global_places_table(conn)
    if mapping is None or limit <= 0:
        return []
    pat = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    prefix = q.replace("%", "").replace("_", "") + "%"
    country_expr = f"COALESCE({mapping['country']}, '')" if mapping["country"] else "''"
    pop_expr = mapping["population"] or "0"
    sql = (f"SELECT {mapping['name']} AS name, {country_expr} AS country, "
           f"{mapping['lat']} AS latitude, {mapping['lon']} AS longitude, "
           f"{pop_expr} AS population FROM global_places "
           f"WHERE LOWER({mapping['name']}) LIKE LOWER(?) ESCAPE '\\' "
           f"ORDER BY CASE WHEN LOWER({mapping['name']}) LIKE LOWER(?) "
           f"THEN 0 ELSE 1 END, {pop_expr} DESC LIMIT ?")
    out = []
    for r in conn.execute(sql, (pat, prefix, limit)).fetchall():
        d = dict(r)
        out.append({
            "kind": "global_registry", "source": "global_registry",
            "collected": False, "location_id": None,
            "name": d["name"], "country": d["country"],
            "latitude": d["latitude"], "longitude": d["longitude"],
            "population": d["population"], "timezone": None,
        })
    return out


def _cities_registered(conn) -> Optional[int]:
    if _global_places_table(conn) is None:
        return None
    return conn.execute("SELECT COUNT(*) FROM global_places").fetchone()[0]


@router.get("/search")
def search(q: str = Query(..., min_length=1),
           conn=Depends(_conn)) -> Dict[str, Any]:
    """Resolve a place name: collected locations first, then the
    GeoNames global_places registry, then Open-Meteo geocoding.

    Any real city on Earth resolves: collected locations are tagged
    kind="collected_location" (stored history + predictions); registry
    hits are labelled source="global_registry"; geocoding candidates are
    tagged kind="geocoding_candidate". Registry and geocoding hits
    resolve through the on-demand live path of /v1/global/weather and
    are never silently turned into collected locations.
    """
    pat = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    rows = conn.execute(
        f"SELECT {_LOCATION_COLS} FROM locations "
        "WHERE LOWER(name) LIKE LOWER(?) ESCAPE '\\' "
        "OR LOWER(COALESCE(country, '')) LIKE LOWER(?) ESCAPE '\\' "
        "ORDER BY CASE WHEN LOWER(name) LIKE LOWER(?) THEN 0 ELSE 1 END, name "
        "LIMIT 5",
        (pat, pat, q.replace("%", "") + "%")).fetchall()
    results: List[Dict[str, Any]] = []
    seen_coords = set()
    seen_names = set()
    for r in rows:
        d = dict(r)
        seen_coords.add((round(d["latitude"], 2), round(d["longitude"], 2)))
        seen_names.add(((d["name"] or "").lower(), (d["country"] or "").lower()))
        results.append({
            "kind": "collected_location", "source": "collected_locations",
            "collected": True,
            "location_id": d["id"], "name": d["name"],
            "country": d["country"], "latitude": d["latitude"],
            "longitude": d["longitude"], "timezone": d["timezone"],
            "is_coastal": bool(d["is_coastal"]),
        })
    if len(results) < 5:
        for c in _registry_matches(conn, q, 5 - len(results)):
            key = (round(c["latitude"] or 0, 2), round(c["longitude"] or 0, 2))
            name_key = ((c["name"] or "").lower(), (c["country"] or "").lower())
            if key in seen_coords or name_key in seen_names:
                continue
            seen_coords.add(key)
            seen_names.add(name_key)
            results.append(c)
    geocoding_error = None
    if len(results) < 5:
        candidates, geocoding_error = _geocode(q)
        for c in candidates:
            c = {**c, "source": "open_meteo_geocoding"}
            key = (round(c["latitude"] or 0, 2), round(c["longitude"] or 0, 2))
            name_key = ((c["name"] or "").lower(), (c["country"] or "").lower())
            if key in seen_coords or name_key in seen_names:
                continue
            seen_coords.add(key)
            seen_names.add(name_key)
            results.append(c)
            if len(results) >= 5:
                break
    return {
        "query": q,
        "results": results[:5],
        "geocoding_error": geocoding_error,
        "note": "Collected locations carry stored history and ML "
                "predictions; global-registry and geocoding candidates "
                "are served on demand by live fetch and are not stored.",
        "provenance": provenance(
            ["aether-db", "geonames-registry", "open-meteo-geocoding"], _now()),
    }


# ---------------- unified weather ------------------------------------------

@router.get("/weather")
def weather(lat: float = Query(..., ge=-90, le=90),
            lon: float = Query(..., ge=-180, le=180),
            conn=Depends(_conn)) -> Dict[str, Any]:
    now_s = _now()
    loc, dist = _nearest_location(conn, lat, lon)

    if loc is not None:
        lid = loc["id"]
        obs = conn.execute(
            "SELECT station, observed_at, temperature, humidity, pressure, "
            "wind_speed, wind_direction, precipitation, source, quality, "
            "collected_at FROM weather_observations WHERE location_id = ? "
            "ORDER BY observed_at DESC LIMIT 1", (lid,)).fetchone()
        observation = {"kind": "observation", **dict(obs)} if obs else None

        # Latest forecast_run per model first (index-only scan), then a
        # direct seek per (model, run). A correlated MAX() subquery over
        # the full history of a 5-minute-tier location takes minutes on
        # the production DB — measured, not assumed.
        runs: Dict[str, str] = {}
        for r in conn.execute(
                "SELECT model, MAX(forecast_run) AS run "
                "FROM weather_forecasts WHERE location_id = ? "
                "GROUP BY model", (lid,)).fetchall():
            runs[r["model"]] = r["run"]
        model_rows: Dict[str, List[Dict[str, Any]]] = {}
        for model, run in runs.items():
            rows = conn.execute(
                f"SELECT {_FORECAST_COLS} FROM weather_forecasts "
                "WHERE location_id = ? AND model = ? AND forecast_run = ? "
                "ORDER BY valid_time", (lid, model, run)).fetchall()
            model_rows[model] = [dict(r) for r in rows]
        forecast = _shape_forecast(model_rows) if model_rows else None
        if forecast is not None:
            forecast["forecast_run"] = runs

        ocean = None
        oc = conn.execute(
            "SELECT model, valid_time, wave_height, wave_period, "
            "wave_direction, sea_surface_temperature, collected_at "
            "FROM ocean_forecasts WHERE location_id = ? "
            "ORDER BY valid_time DESC LIMIT 96", (lid,)).fetchall()
        if oc:
            pts = [dict(r) for r in reversed(oc)]
            ocean = {"kind": "ocean_forecast", "latest": pts[-1],
                     "points": pts[-48:]}

        newest = conn.execute(
            "SELECT MAX(collected_at) AS t FROM ("
            "  SELECT collected_at FROM weather_forecasts WHERE location_id = ?"
            "  UNION ALL"
            "  SELECT collected_at FROM weather_observations WHERE location_id = ?"
            ")", (lid, lid)).fetchone()["t"]
        age = age_minutes(newest) if newest else None
        return {
            "query": {"latitude": lat, "longitude": lon},
            "source": "collected",
            "location": {**loc, "is_coastal": bool(loc["is_coastal"]),
                         "distance_km": dist},
            "staleness": {"newest_data_at": newest,
                          "data_age_min": round(age, 1) if age is not None else None},
            "observation": observation,
            "forecast": forecast,
            "predictions": _predictions_block(conn, lid, now_s),
            "alerts": _active_alerts(conn, now_s, lid),
            "ocean": ocean,
            "provenance": provenance(["aether-db"], newest),
        }

    # No collected location nearby: live fetch, nothing stored.
    try:
        live = _live_fetch(lat, lon)
    except Exception as exc:
        raise HTTPException(
            502, f"no collected location within {NEAR_KM:.0f} km and the "
                 f"live fetch failed: {exc}")
    current = None
    if live.get("current"):
        current = {"kind": "current_conditions",
                   "note": "Model-derived current conditions from the live "
                           "fetch; not a station observation.",
                   **live["current"]}
    return {
        "query": {"latitude": lat, "longitude": lon},
        "source": "live_fetch",
        "location": None,
        "note": "This point is not a collected location. The forecast was "
                "fetched live from Open-Meteo for these coordinates and "
                "was not stored; there is no stored history, no ML "
                "prediction and no tracked alert coverage for it.",
        "staleness": {"newest_data_at": live["fetched_at"], "data_age_min": 0.0},
        "observation": current,
        "forecast": _shape_forecast(live["models"]),
        "predictions": None,
        "alerts": [],
        "ocean": None,
        "provenance": provenance(["open-meteo-live"], live["fetched_at"]),
    }


# ---------------- overview ---------------------------------------------------

@router.get("/overview")
def overview(conn=Depends(_conn)) -> Dict[str, Any]:
    now_s = _now()
    pred_locs = {r[0] for r in conn.execute(
        "SELECT DISTINCT location_id FROM ml_batch_predictions").fetchall()}
    locs = []
    for r in conn.execute(
            f"SELECT {_LOCATION_COLS} FROM locations ORDER BY name").fetchall():
        d = dict(r)
        d["is_coastal"] = bool(d["is_coastal"])
        d["has_predictions"] = d["id"] in pred_locs
        locs.append(d)

    status_by_component: Dict[str, Dict[str, Any]] = {}
    for r in conn.execute(
            "SELECT component, status, started_at, ended_at, rows_written "
            "FROM ingestion_runs ORDER BY ended_at DESC LIMIT 1000").fetchall():
        d = dict(r)
        status_by_component.setdefault(d["component"], {
            "component": d["component"], "status": d["status"],
            "started_at": d["started_at"], "ended_at": d["ended_at"],
            "rows_written": d["rows_written"]})

    active = _active_alerts(conn, now_s)
    n = len(locs)
    with_pred = sum(1 for d in locs if d["has_predictions"])
    return {
        "as_of": now_s,
        "coverage": {
            "collected_locations": n,
            "cities_registered": _cities_registered(conn),
            "cities_note": "Cities in the GeoNames global_places registry "
                           "(searchable by name; weather served on demand "
                           "by live fetch). Null until the registry is "
                           "loaded.",
            "collected_note": "Locations with stored multi-model forecast "
                              "history, observations and (where available) "
                              "ML predictions in the AETHER database.",
            "on_demand": {
                "available": True,
                "scope": "any coordinates on Earth",
                "search": "any city name via Open-Meteo geocoding",
                "weather": "live Open-Meteo fetch for any lat/lon not "
                           "near a collected location",
                "note": "On-demand answers are fetched live and are NOT "
                        "stored: no history, no ML predictions, no "
                        "tracked alerts for uncollected points.",
            },
        },
        "locations": locs,
        "collection_status": sorted(status_by_component.values(),
                                    key=lambda d: d["component"]),
        "active_alert_count": len(active),
        "active_alerts": active,
        "prediction_coverage": {
            "locations_with_predictions": with_pred,
            "collected_locations": n,
            "fraction": round(with_pred / n, 3) if n else None,
        },
        "provenance": provenance(["aether-db"], now_s),
    }


# ---------------- dashboard --------------------------------------------------

_DASHBOARD_HEAD = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AETHER — Global Weather System</title>
<style>
body{font-family:system-ui,sans-serif;margin:2rem;background:#0b1020;color:#e6e9f2}
h1{font-size:1.4rem} h2{font-size:1.1rem;margin-top:2rem}
table{border-collapse:collapse;width:100%;background:#121830}
th,td{padding:.45rem .6rem;border-bottom:1px solid #26305a;text-align:left;font-size:.9rem}
td.num,th.num{text-align:right}
tr.alert td{background:#3a1420}
.badge{background:#b3001b;color:#fff;border-radius:.5rem;padding:.05rem .5rem;font-size:.75rem}
.meta{color:#9aa5c7;font-size:.9rem}
input,button{padding:.4rem;background:#1a2142;color:#e6e9f2;border:1px solid #33407a;border-radius:.3rem}
pre{background:#121830;padding:1rem;overflow:auto;max-height:24rem;font-size:.8rem}
a{color:#8ab4ff}
</style></head><body>
<h1>AETHER — Global Weather System</h1>
"""

_LOOKUP_JS = """
<h2>Coordinate / city lookup (on-demand, any place on Earth)</h2>
<p class="meta">Collected locations answer from stored data; any other
point is fetched live from Open-Meteo and is not stored.</p>
<p><input id="q" placeholder="City name (e.g. Paris)" size="24">
<button onclick="doSearch()">Search</button>
<input id="lat" placeholder="Latitude" size="10">
<input id="lon" placeholder="Longitude" size="10">
<button onclick="doWeather()">Get weather</button></p>
<pre id="out">Results appear here.</pre>
<script>
function esc(s){return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;");}
async function doSearch(){
  const q=document.getElementById("q").value;
  const r=await fetch("/v1/global/search?q="+encodeURIComponent(q));
  const j=await r.json();
  let h="";
  for(const c of j.results){
    h+=esc(c.name||"")+" — "+esc(c.country||"")+" ("+esc(c.latitude)+", "+
       esc(c.longitude)+") ["+esc(c.kind)+"]\\n";
  }
  document.getElementById("out").textContent=h||"No results.";
}
async function doWeather(){
  const lat=document.getElementById("lat").value;
  const lon=document.getElementById("lon").value;
  const r=await fetch("/v1/global/weather?lat="+encodeURIComponent(lat)+
                       "&lon="+encodeURIComponent(lon));
  const j=await r.json();
  document.getElementById("out").textContent=JSON.stringify(j,null,1);
}
</script>
</body></html>
"""


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(conn=Depends(_conn)) -> str:
    now_s = _now()
    locs = [dict(r) for r in conn.execute(
        f"SELECT {_LOCATION_COLS} FROM locations ORDER BY name").fetchall()]

    latest_obs: Dict[int, Dict[str, Any]] = {}
    for r in conn.execute(
            "SELECT o.location_id, o.temperature, o.observed_at "
            "FROM weather_observations o JOIN ("
            "  SELECT location_id, MAX(observed_at) AS m "
            "  FROM weather_observations GROUP BY location_id) t "
            "ON t.location_id = o.location_id AND t.m = o.observed_at"
            ).fetchall():
        latest_obs.setdefault(r["location_id"], dict(r))

    next_pred: Dict[int, Dict[str, Any]] = {}
    for r in conn.execute(
            "SELECT p.location_id, p.prediction, p.valid_time, "
            "p.model_version FROM ml_batch_predictions p JOIN ("
            "  SELECT location_id, MIN(valid_time) AS m "
            "  FROM ml_batch_predictions WHERE task = 'temperature' "
            "  AND valid_time >= ? GROUP BY location_id) t "
            "ON t.location_id = p.location_id AND t.m = p.valid_time "
            "WHERE p.task = 'temperature'", (now_s,)).fetchall():
        next_pred.setdefault(r["location_id"], dict(r))

    alerts = _active_alerts(conn, now_s)
    alert_counts: Dict[int, int] = {}
    for a in alerts:
        if a.get("location_id") is not None:
            alert_counts[a["location_id"]] = alert_counts.get(a["location_id"], 0) + 1

    def e(v: Any) -> str:
        return html.escape("—" if v is None else str(v))

    cities = _cities_registered(conn)
    cities_txt = (f" · Registered cities (GeoNames): <b>{cities}</b>"
                  if cities is not None else "")
    parts = [_DASHBOARD_HEAD]
    parts.append(
        f"<p class='meta'>Collected locations: <b>{len(locs)}</b> "
        "(stored history + ML predictions where available)" + cities_txt +
        " · On-demand coverage: <b>any city or coordinates worldwide</b> "
        "via live Open-Meteo fetch (not stored) · as of " + e(now_s) + "</p>")
    if alerts:
        parts.append("<h2>Active alerts</h2><table>"
                     "<tr><th>Type</th><th>Severity</th><th>Title</th>"
                     "<th>Issued</th></tr>")
        for a in alerts:
            parts.append(
                "<tr class='alert'><td>" + e(a["type"]) + "</td><td>" +
                e(a["severity"]) + "</td><td>" + e(a["title"]) +
                "</td><td>" + e(a["issued_at"]) + "</td></tr>")
        parts.append("</table>")
    parts.append("<h2>Collected locations</h2><table><tr><th>Location</th>"
                 "<th>Country</th><th class='num'>Lat</th>"
                 "<th class='num'>Lon</th><th class='num'>Latest obs °C</th>"
                 "<th>Observed at</th><th class='num'>Next ML temp pred °C</th>"
                 "<th>Alerts</th></tr>")
    for d in locs:
        obs = latest_obs.get(d["id"])
        pred = next_pred.get(d["id"])
        n_alerts = alert_counts.get(d["id"], 0)
        row_cls = " class='alert'" if n_alerts else ""
        parts.append(
            f"<tr{row_cls}><td>" + e(d["name"]) + "</td><td>" +
            e(d["country"]) + "</td><td class='num'>" +
            e(round(d["latitude"], 3)) + "</td><td class='num'>" +
            e(round(d["longitude"], 3)) + "</td><td class='num'>" +
            e(obs["temperature"] if obs else None) + "</td><td>" +
            e(obs["observed_at"] if obs else None) + "</td><td class='num'>" +
            e(round(pred["prediction"], 1) if pred and pred["prediction"] is not None else None) +
            "</td><td>" +
            (f"<span class='badge'>{n_alerts}</span>" if n_alerts else "—") +
            "</td></tr>")
    parts.append("</table>")
    parts.append(_LOOKUP_JS)
    return "".join(parts)
