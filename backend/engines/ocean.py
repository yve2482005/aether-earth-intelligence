"""Ocean engine (spec §30) — real collector on the free Open-Meteo marine API.

Endpoint verified live 2026-10-05: https://marine-api.open-meteo.com/v1/marine
returns hourly wave_height (m), wave_period (s), wave_direction (deg) and
sea_surface_temperature (C). Same provider family as the weather collector,
no key, no spend.

Stores into ocean_forecasts. Query helpers serve the /v1/ocean API.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional

from backend.common.config import HTTP_TIMEOUT
from backend.common.http import fetch_json, make_session
from backend.common.logging_setup import setup_logging
from backend.common.timeutil import parse_iso, sql_now_iso, utc_iso

log = setup_logging("aether.engines.ocean")

MARINE_URL = "https://marine-api.open-meteo.com/v1/marine"
HOURLY_VARS = "wave_height,wave_period,wave_direction,sea_surface_temperature"

INSERT_SQL = """INSERT OR IGNORE INTO ocean_forecasts
(location_id, source, model, valid_time, wave_height, wave_period,
 wave_direction, sea_surface_temperature, collected_at)
VALUES (?,?,?,?,?,?,?,?,?)"""


def _record_health(conn: sqlite3.Connection, ok: bool, latency_ms: Optional[float],
                   records: int, message: Optional[str]) -> None:
    conn.execute(
        """INSERT INTO system_health (component, status, last_success, last_failure,
                                      latency_ms, records, message, checked_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        ("collector:openmeteo-marine", "ok" if ok else "down",
         utc_iso() if ok else None, None if ok else utc_iso(),
         latency_ms, records, message, utc_iso()))
    conn.commit()


def collect(conn: sqlite3.Connection, location_id: int, lat: float, lon: float,
            session=None) -> Dict[str, Any]:
    """Fetch marine forecast and store it. Never raises on HTTP failure."""
    session = session or make_session()
    try:
        from backend.db.migrate import ensure_v2
        ensure_v2(conn)  # idempotent
    except Exception:
        pass
    try:
        res = fetch_json(session, MARINE_URL, params={
            "latitude": lat, "longitude": lon, "hourly": HOURLY_VARS,
            "timezone": "UTC", "forecast_days": 7,
        }, timeout=HTTP_TIMEOUT)
    except Exception as e:  # fetch_json only traps requests errors; belt-and-braces
        res = {"ok": False, "latency_ms": None, "error": f"{type(e).__name__}: {e}"}
    if not res["ok"]:
        _record_health(conn, False, res["latency_ms"], 0, res["error"])
        return {"ok": False, "error": res["error"]}
    hourly = (res["data"] or {}).get("hourly") or {}
    times = hourly.get("time") or []
    wh = hourly.get("wave_height") or []
    wp = hourly.get("wave_period") or []
    wd = hourly.get("wave_direction") or []
    sst = hourly.get("sea_surface_temperature") or []
    collected = utc_iso()
    params = [(
        location_id, "open-meteo-marine", "marine_best_match",
        utc_iso(parse_iso(t)),
        wh[i] if i < len(wh) else None, wp[i] if i < len(wp) else None,
        wd[i] if i < len(wd) else None, sst[i] if i < len(sst) else None,
        collected) for i, t in enumerate(times)]
    inserted = 0
    if params:
        cur = conn.executemany(INSERT_SQL, params)
        inserted = cur.rowcount if cur.rowcount is not None else 0
    conn.commit()
    _record_health(conn, True, res["latency_ms"], inserted, None)
    log.info("openmeteo-marine: %d rows", inserted)
    return {"ok": True, "inserted": inserted, "total": len(times)}


def latest(conn: sqlite3.Connection, location_id: int, hours: int = 48) -> Dict[str, Any]:
    """Most recent marine forecast rows for a location."""
    now_m2h = sql_now_iso("-2 hours")
    now_ph = sql_now_iso(f"+{hours} hours")
    rows = conn.execute(
        """SELECT * FROM ocean_forecasts WHERE location_id = ?
           AND valid_time BETWEEN """ + now_m2h + " AND " + now_ph + """
           ORDER BY valid_time""",
        (location_id,)).fetchall()
    items = [dict(r) for r in rows]
    if not items:
        return {"status": "unavailable", "kind": "FORECAST",
                "message": ("No ocean data for this location yet. "
                            "Run the Open-Meteo marine collector first."),
                "items": [],
                "provenance": {"sources": ["open-meteo-marine"], "updated_at": None}}
    for it in items:
        it["kind"] = "FORECAST"
    return {"location_id": location_id, "count": len(items), "items": items,
            "units": {"wave_height": "m", "wave_period": "s",
                      "wave_direction": "deg", "sea_surface_temperature": "C"},
            "kind": "FORECAST",
            "provenance": {"sources": ["open-meteo-marine"],
                           "updated_at": max(r["collected_at"] for r in items)}}
