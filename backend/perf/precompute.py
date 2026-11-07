"""Precomputation (§§14-15, 47-48): per-location home bundles.

GET /v1/home/{location_id} returns weather + hourly + daily + AQI + alerts +
quake-proximity + tsunami + confidence in ONE response (?level=summary|details
per §34). Bundles are assembled from the DB (already collected), not by
calling sources at request time — so the endpoint is fast by construction.
Saved-location priority refresh ordering: the collector refreshes saved
locations before ad-hoc ones.

Life-safety honesty: quake/tsunami entries carry a `freshness` block; when
the underlying fetch freshness cannot be verified, the bundle says so
explicitly (§66) instead of presenting stale data as live.
"""
from __future__ import annotations

import math
import sqlite3
from typing import Any, Dict, List, Optional

from backend.common.logging_setup import setup_logging
from backend.common.timeutil import utc_iso
from backend.perf import cache as cache_mod

log = setup_logging("aether.perf.precompute")

HOME_BUNDLE_TTL = 300.0  # non-critical bundle parts may be cached 5 min


def _haversine_km(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def ensure_bundle_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS perf_home_bundles (
            location_id INTEGER PRIMARY KEY,
            level TEXT NOT NULL,
            bundle_json TEXT NOT NULL,
            built_at TEXT NOT NULL,
            valid_until TEXT NOT NULL
        )""")
    conn.commit()


def _latest_health(conn, component_prefix: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        """SELECT component, status, checked_at, latency_ms, records, message
           FROM system_health WHERE component LIKE ? ORDER BY checked_at DESC LIMIT 1""",
        (component_prefix + "%",)).fetchone()
    return dict(row) if row else None


def _freshness_block(health: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    # §66: when freshness cannot be verified, say so explicitly.
    unverifiable = {"verified": False, "status": "could_not_verify",
                    "note": "CURRENT STATUS COULD NOT BE VERIFIED — no freshness "
                            "evidence available for this feed; treating as stale-safe."}
    if not health:
        return unverifiable
    ok = health.get("status") == "ok"
    if ok:
        return {"verified": True, "status": "verified",
                "checked_at": health.get("checked_at"), "note": None}
    return dict(unverifiable, checked_at=health.get("checked_at"))


def build_home_bundle(conn: sqlite3.Connection, location_id: int,
                      level: str = "summary") -> Dict[str, Any]:
    """Assemble the home bundle for a location from stored data only."""
    loc = conn.execute("SELECT * FROM locations WHERE id=?", (location_id,)).fetchone()
    if loc is None:
        raise KeyError(f"location {location_id} not found")
    loc = dict(loc)
    lat, lon = loc["latitude"], loc["longitude"]

    hourly = [dict(r) for r in conn.execute(
        """SELECT valid_time, temperature, feels_like, humidity, pressure,
                  wind_speed, wind_direction, precipitation, precip_probability,
                  cloud_cover, aqi, pm2_5, pm10
           FROM weather_forecasts
           WHERE location_id=? AND model IN ('gfs','icon_seamless')
           ORDER BY valid_time ASC LIMIT ?""",
        (location_id, 48 if level == "details" else 12))]
    daily = [dict(r) for r in conn.execute(
        """SELECT date, sunrise, sunset FROM weather_daily
           WHERE location_id=? ORDER BY date ASC LIMIT 7""", (location_id,))]

    alerts = [dict(r) for r in conn.execute(
        """SELECT alert_id, type, severity, title, message, source, kind,
                  issued_at, expires_at, status
           FROM alerts WHERE location_id=? AND status='active'
           ORDER BY severity DESC, issued_at DESC LIMIT 20""", (location_id,))]

    quakes = [dict(r) for r in conn.execute(
        """SELECT canonical_event_id, magnitude, latitude AS qlat, longitude AS qlon,
                  depth, origin_time, region, alert_level, tsunami_flag
           FROM earthquake_events
           WHERE origin_time > strftime('%Y-%m-%dT%H:%M:%SZ','now','-30 days')
           ORDER BY origin_time DESC LIMIT 50""")]
    nearby = []
    for q in quakes:
        if q["qlat"] is None or q["qlon"] is None:
            continue
        d = _haversine_km(lat, lon, q["qlat"], q["qlon"])
        if d <= 2000:
            q["distance_km"] = round(d, 1)
            nearby.append(q)
    nearby.sort(key=lambda q: q["distance_km"])

    tsunami = [dict(r) for r in conn.execute(
        """SELECT tsunami_event_id, message_type, severity, affected_region,
                  issue_time, arrival_time, status
           FROM tsunami_events WHERE status='active' ORDER BY issue_time DESC LIMIT 10""")]

    bundle = {
        "location": {"id": loc["id"], "name": loc.get("name"),
                     "latitude": lat, "longitude": lon,
                     "timezone": loc.get("timezone"), "country": loc.get("country")},
        "level": level,
        "weather": {"hourly": hourly, "daily": daily},
        "aqi": {"pm2_5": hourly[0].get("pm2_5") if hourly else None,
                "pm10": hourly[0].get("pm10") if hourly else None,
                "aqi_index": hourly[0].get("aqi") if hourly else None},
        "alerts": alerts,
        "quake_proximity": {"nearest_km": nearby[0]["distance_km"] if nearby else None,
                            "events_within_2000km": nearby[: (10 if level == "details" else 3)]},
        "tsunami": tsunami,
        "freshness": {
            "weather": _freshness_block(_latest_health(conn, "openmeteo")),
            "earthquake": _freshness_block(_latest_health(conn, "usgs")),
            "tsunami": _freshness_block(_latest_health(conn, "tsunami")),
            "bundle_built_at": utc_iso(),
        },
        "confidence": {"kind": "bundle", "parts_cached": False},
        "provenance": {"sources": ["aether-db"], "built_at": utc_iso()},
    }
    return bundle


def store_bundle(conn: sqlite3.Connection, location_id: int, level: str,
                 bundle: Dict[str, Any], valid_seconds: float = HOME_BUNDLE_TTL) -> None:
    import json
    from datetime import datetime, timedelta, timezone
    ensure_bundle_table(conn)
    now = datetime.now(timezone.utc)
    conn.execute(
        """INSERT INTO perf_home_bundles (location_id, level, bundle_json, built_at, valid_until)
           VALUES (?,?,?,?,?)
           ON CONFLICT(location_id) DO UPDATE SET
             level=excluded.level, bundle_json=excluded.bundle_json,
             built_at=excluded.built_at, valid_until=excluded.valid_until""",
        (location_id, level, json.dumps(bundle, default=str),
         now.isoformat(timespec="seconds"),
         (now + timedelta(seconds=valid_seconds)).isoformat(timespec="seconds")))
    conn.commit()


def get_bundle(conn: sqlite3.Connection, location_id: int,
               level: str = "summary") -> Optional[Dict[str, Any]]:
    """Return a cached bundle if still valid, else None."""
    import json
    ensure_bundle_table(conn)
    row = conn.execute(
        "SELECT bundle_json, valid_until FROM perf_home_bundles WHERE location_id=?",
        (location_id,)).fetchone()
    if row is None:
        return None
    from datetime import datetime, timezone
    try:
        if datetime.fromisoformat(row["valid_until"]).replace(tzinfo=timezone.utc) \
                <= datetime.now(timezone.utc):
            return None
    except ValueError:
        return None
    try:
        return json.loads(row["bundle_json"])
    except ValueError:
        return None


def saved_location_refresh_order(conn: sqlite3.Connection) -> List[int]:
    """Saved locations first (priority desc), then any ad-hoc locations."""
    rows = conn.execute(
        """SELECT DISTINCT l.id, COALESCE(sl.priority, 0) AS prio
           FROM locations l LEFT JOIN saved_locations sl
             ON sl.latitude = l.latitude AND sl.longitude = l.longitude
           ORDER BY prio DESC, l.id ASC""").fetchall()
    return [r["id"] for r in rows]
