"""Smart home summary (spec §98): a concise daily summary generated from REAL
collected data only — weather, severe weather, quake activity, tsunami status,
AQI, regional hazards. If nothing meaningful happened, it says so; no
artificial news is ever invented."""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional

from backend.common.timeutil import utc_iso


def _latest_forecast(conn: sqlite3.Connection,
                     location_id: int) -> Optional[Dict[str, Any]]:
    return conn.execute(
        """SELECT * FROM weather_forecasts
           WHERE location_id = ?
           ORDER BY valid_time DESC LIMIT 1""", (location_id,)).fetchone()


def daily_summary(conn: sqlite3.Connection,
                  location_id: Optional[int] = None,
                  user_id: str = "local") -> Dict[str, Any]:
    """Build the daily summary dict. Sections carry `kind` tags; every number
    comes from the DB. `data_quality` notes any stale/missing source."""
    from backend.alerting.preferences import get_travel, travel_active
    sections: List[Dict[str, Any]] = []
    meaningful = False

    loc = None
    if location_id:
        r = conn.execute("SELECT * FROM locations WHERE id = ?",
                         (location_id,)).fetchone()
        loc = dict(r) if r else None
    loc_name = loc["name"] if loc else "your area"

    # --- weather now / next 24h ---
    if location_id:
        fc = _latest_forecast(conn, location_id)
        if fc:
            fc = dict(fc)
            sections.append({
                "kind": "FORECAST",
                "title": f"Weather — {loc_name}",
                "text": (f"{fc.get('temperature')}°C, wind "
                         f"{fc.get('wind_speed')} km/h, rain probability "
                         f"{fc.get('precip_probability')}%, valid {fc.get('valid_time')}. "
                         f"Model: {fc.get('model')}."),
                "updated_at": fc.get("collected_at"),
            })

    # --- severe weather / official alerts active ---
    off = conn.execute(
        """SELECT COUNT(*) AS c FROM alerts
           WHERE status='active' AND kind='official'
             AND severity IN ('warning','critical')""").fetchone()["c"]
    if off:
        meaningful = True
        sections.append({
            "kind": "WARNING",
            "title": "Official warnings active",
            "text": f"{off} official warning-level alert(s) are currently active. See the Alerts tab.",
        })

    # --- quake activity last 24h (M>=4.5) ---
    quakes = conn.execute(
        """SELECT COUNT(*) AS c, MAX(magnitude) AS mmax FROM earthquake_events
           WHERE origin_time >= strftime('%Y-%m-%dT%H:%M:%SZ','now','-1 day') AND magnitude >= 4.5"""
    ).fetchone()
    if quakes and quakes["c"]:
        meaningful = True
        sections.append({
            "kind": "DETECTION",
            "title": "Earthquake activity (24h)",
            "text": (f"{quakes['c']} earthquake(s) of M4.5+ detected worldwide in the "
                     f"last 24 hours; largest M{quakes['mmax']}. Detections of past "
                     f"events — not predictions."),
        })

    # --- tsunami status ---
    tsu = conn.execute(
        "SELECT COUNT(*) AS c FROM tsunami_events WHERE status='active'").fetchone()["c"]
    sections.append({
        "kind": "OBSERVATION",
        "title": "Tsunami status",
        "text": (f"{tsu} active official tsunami message(s)." if tsu
                 else "No active official tsunami messages."),
    })
    if tsu:
        meaningful = True

    # --- AQI ---
    if location_id:
        fc = _latest_forecast(conn, location_id)
        if fc and fc.get("aqi") is not None:
            aqi = fc["aqi"]
            sections.append({
                "kind": "OBSERVATION",
                "title": "Air quality",
                "text": f"AQI {aqi:.0f} at {loc_name} (latest forecast run).",
            })
            if aqi >= 100:
                meaningful = True

    # --- regional hazards: active non-info alerts ---
    hz = conn.execute(
        """SELECT COUNT(*) AS c FROM alerts
           WHERE status='active' AND severity != 'info'""").fetchone()["c"]
    if hz:
        meaningful = True
        sections.append({
            "kind": "WARNING",
            "title": "Active hazards",
            "text": f"{hz} active alert(s) above info level. See the Alerts tab.",
        })

    # --- travel destination context ---
    travel = get_travel(conn, user_id)
    if travel_active(travel):
        sections.append({
            "kind": "FORECAST",
            "title": f"Travel — {travel['destination_name']}",
            "text": (f"Monitoring {travel['destination_name']} "
                     f"({travel['start_date']} → {travel['end_date']}). "
                     f"Destination weather, quake, tsunami and AQI appear here when "
                     f"data is collected for the nearest station."),
        })

    # --- data quality honesty note ---
    stale = conn.execute(
        """SELECT component, checked_at FROM system_health
           WHERE checked_at = (SELECT MAX(checked_at) FROM system_health s2
                               WHERE s2.component = system_health.component)
             AND status != 'ok'""").fetchall()
    if stale:
        sections.append({
            "kind": "OBSERVATION",
            "title": "Data quality note",
            "text": ("Some sources are not reporting normally: " +
                     ", ".join(f"{r['component']} ({r['checked_at']})" for r in stale) +
                     ". Figures above may be incomplete."),
        })

    headline = ("Significant activity in the last 24 hours."
                if meaningful else
                "Nothing significant in the last 24 hours.")
    return {"headline": headline, "meaningful": meaningful,
            "location": loc_name, "sections": sections,
            "generated_at": utc_iso(),
            "provenance": ["aether-summary"]}
