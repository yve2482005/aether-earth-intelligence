"""Flood engine (spec §28) — hazard / exposure / risk kept separate.

Honesty contract: with no river-gauge, soil-moisture, or terrain data wired,
this engine CANNOT assess flood risk. A rainfall forecast alone never makes an
area "flooding". The three components are reported independently:

- hazard: what the NWP rainfall forecast says (kind=FORECAST), or unavailable.
- exposure: insufficient data (no population/infrastructure layers wired).
- risk: cannot be assessed until hazard AND exposure are both real.

Nothing here is a flood warning. Official flood warnings outrank everything.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, Optional

from backend.common.timeutil import sql_now_iso


def assess(conn: sqlite3.Connection, location_id: int, hours: int = 72) -> Dict[str, Any]:
    now_m2h = sql_now_iso("-2 hours")
    now_ph = sql_now_iso(f"+{hours} hours")
    row = conn.execute(
        """SELECT SUM(COALESCE(precipitation, 0)) AS precip_sum,
                  MAX(precipitation) AS precip_max,
                  MAX(precip_probability) AS prob_max,
                  MAX(collected_at) AS collected_at, COUNT(*) AS n
           FROM weather_forecasts
           WHERE location_id = ? AND source = 'open-meteo'
             AND valid_time BETWEEN """ + now_m2h + " AND " + now_ph,
        (location_id,)).fetchone()
    row = dict(row) if row else {}
    n = row.get("n") or 0

    hazard: Dict[str, Any]
    if n == 0:
        hazard = {"status": "unavailable", "kind": "FORECAST",
                  "message": "No rainfall forecast data for this location."}
    else:
        hazard = {
            "status": "rainfall_forecast_only",
            "kind": "FORECAST",
            "precip_sum_mm": round(row.get("precip_sum") or 0.0, 1),
            "precip_max_hourly_mm": row.get("precip_max"),
            "precip_probability_max": row.get("prob_max"),
            "window_hours": hours,
            "note": ("NWP rainfall forecast only. Rainfall alone does not determine "
                     "flooding — river levels, soil saturation, drainage, and terrain "
                     "matter, and none of those are wired yet."),
        }

    exposure = {
        "status": "insufficient_data",
        "kind": "INFORMATION",
        "message": ("No exposure layers (population, infrastructure, elevation, "
                    "drainage) are wired, so exposure cannot be evaluated."),
    }

    risk = {
        "status": "cannot_assess",
        "kind": "RISK",
        "message": ("Flood risk cannot be assessed: hazard is rainfall-only and "
                    "exposure data is missing. This area is NOT labeled flooding, "
                    "at-risk, or safe — the data to decide does not exist here. "
                    "Follow official flood warnings from your national service."),
    }

    return {
        "location_id": location_id,
        "hazard": hazard,
        "exposure": exposure,
        "risk": risk,
        "kind": "RISK",
        "provenance": {"sources": ["open-meteo"] if n else [],
                       "updated_at": row.get("collected_at")},
    }
