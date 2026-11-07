"""Nowcast engine (spec §24).

0–120 minute architecture in four windows: 0–15, 15–30, 30–60, 60–120 min.
A nowcast is an observation-anchored extrapolation: the most recent real
observation blended with the NWP background, with the observation weight
decaying as lead time grows.

Honest degradation (spec §1 — NOWCAST vs NWP FORECAST must be explicit):
- With a usable recent observation (<=60 min old): windows are kind=NOWCAST.
- Without one: status="nowcast_unavailable", message "nowcast unavailable —
  showing NWP forecast", and every window is kind=NWP FORECAST.

Never invents observations. Never labels NWP output as NOWCAST.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional

from backend.common.timeutil import age_minutes, now_utc, parse_iso, sql_now_iso, utc_iso

WINDOWS = [
    {"id": "0-15", "lead_min": 7.5, "start_min": 0, "end_min": 15},
    {"id": "15-30", "lead_min": 22.5, "start_min": 15, "end_min": 30},
    {"id": "30-60", "lead_min": 45.0, "start_min": 30, "end_min": 60},
    {"id": "60-120", "lead_min": 90.0, "start_min": 60, "end_min": 120},
]

VARS = ["temperature", "humidity", "pressure", "wind_speed", "precipitation"]

# Observation is trusted fully at t=0 and linearly phased out by 120 min.
OBS_WEIGHT_HORIZON_MIN = 120.0
MAX_OBS_AGE_MIN = 60.0


def _latest_observation(conn: sqlite3.Connection, location_id: int) -> Optional[dict]:
    row = conn.execute(
        """SELECT * FROM weather_observations WHERE location_id = ?
           ORDER BY observed_at DESC LIMIT 1""", (location_id,)).fetchone()
    return dict(row) if row else None


def _nwp_background(conn: sqlite3.Connection, location_id: int,
                    models: Optional[List[str]] = None) -> List[dict]:
    """NWP rows for the next ~3h, one per valid_time (median across models)."""
    models = models or ["gfs_seamless", "icon_seamless", "ecmwf_ifs", "jma_seamless"]
    placeholders = ",".join("?" * len(models))
    now_m1h = sql_now_iso("-1 hour")
    now_p3h = sql_now_iso("+3 hours")
    rows = conn.execute(
        f"""SELECT valid_time,
                   AVG(temperature) AS temperature, AVG(humidity) AS humidity,
                   AVG(pressure) AS pressure, AVG(wind_speed) AS wind_speed,
                   AVG(precipitation) AS precipitation, AVG(cloud_cover) AS cloud_cover,
                   MAX(collected_at) AS collected_at, COUNT(*) AS n_models
            FROM weather_forecasts
            WHERE location_id = ? AND source = 'open-meteo' AND model IN ({placeholders})
              AND valid_time BETWEEN {now_m1h} AND {now_p3h}
            GROUP BY valid_time ORDER BY valid_time""",
        (location_id, *models)).fetchall()
    return [dict(r) for r in rows]


def build(conn: sqlite3.Connection, location_id: int, location: Optional[dict] = None) -> Dict[str, Any]:
    """Build the nowcast payload for one location."""
    now = now_utc()
    obs = _latest_observation(conn, location_id)
    obs_age = age_minutes(obs["observed_at"]) if obs else None
    anchored = obs is not None and obs_age is not None and obs_age <= MAX_OBS_AGE_MIN

    nwp = _nwp_background(conn, location_id)
    if not nwp:
        return {
            "status": "unavailable",
            "kind": "NOWCAST",
            "message": ("No NWP background and no recent observations for this location — "
                        "neither a nowcast nor an NWP forecast can be shown."),
            "windows": [],
            "provenance": {"sources": ["open-meteo"], "updated_at": None},
        }

    windows = []
    for w in WINDOWS:
        lead = w["lead_min"]
        # NWP value at the window's lead time: nearest row to now+lead.
        best = min(nwp, key=lambda r: abs((parse_iso(r["valid_time"]) -
                                           now).total_seconds() / 60.0 - lead))
        vals: Dict[str, Optional[float]] = {}
        for var in VARS:
            nwp_v = best.get(var)
            obs_v = obs.get(var) if obs else None
            if anchored and obs_v is not None and nwp_v is not None:
                weight = max(0.0, 1.0 - lead / OBS_WEIGHT_HORIZON_MIN)
                vals[var] = round(weight * obs_v + (1.0 - weight) * nwp_v, 2)
            elif anchored and obs_v is not None:
                vals[var] = round(obs_v, 2)  # persistence when NWP lacks the variable
            else:
                vals[var] = nwp_v
        windows.append({
            "window": w["id"],
            "lead_minutes": lead,
            "kind": "NOWCAST" if anchored else "NWP FORECAST",
            "values": vals,
            "nwp_valid_time": best["valid_time"],
            "observation_weight": round(max(0.0, 1.0 - lead / OBS_WEIGHT_HORIZON_MIN), 2)
                                  if anchored else 0.0,
        })

    payload: Dict[str, Any] = {
        "status": "ok" if anchored else "nowcast_unavailable",
        "kind": "NOWCAST" if anchored else "FORECAST",
        "windows": windows,
        "anchored": anchored,
        "observation": None,
        "provenance": {"sources": [], "updated_at": None},
    }
    if anchored:
        payload["observation"] = {
            "observed_at": obs["observed_at"],
            "age_minutes": round(obs_age, 1),
            "source": obs.get("source"),
            "quality": obs.get("quality"),
            "kind": "OBSERVATION",
            "values": {v: obs.get(v) for v in VARS},
        }
        payload["provenance"]["sources"] = ["observation", "open-meteo"]
        payload["provenance"]["updated_at"] = obs["observed_at"]
    else:
        payload["message"] = ("nowcast unavailable — showing NWP forecast. "
                              "No usable recent observation (need one <=60 min old).")
        if obs is not None:
            payload["observation"] = {
                "observed_at": obs["observed_at"],
                "age_minutes": round(obs_age, 1) if obs_age is not None else None,
                "note": "too old to anchor a nowcast",
                "kind": "OBSERVATION",
            }
        latest_collect = max((r.get("collected_at") for r in nwp if r.get("collected_at")),
                             default=None)
        payload["provenance"]["sources"] = ["open-meteo"]
        payload["provenance"]["updated_at"] = latest_collect
    if location:
        payload["location"] = {"id": location.get("id"), "name": location.get("name"),
                               "latitude": location.get("latitude"),
                               "longitude": location.get("longitude")}
    return payload
