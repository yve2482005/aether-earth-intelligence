"""Per-user alert preferences (spec §48), silent hours (§16), travel mode (§99).

Tables:
  alert_thresholds — per user/location: radius, hazard types, min severity,
    temp/rain/wind/AQI/quake-magnitude thresholds, silent-hours config.
    (Named alert_thresholds, not alert_preferences, because another v2
    workstream's DDL already defines alert_preferences with a different,
    notification-routing shape in backend/db/schema.sql.)
  travel_mode — per user: destination + date range for temporary monitoring.

Threshold fields left NULL mean "not set" (no check). All checks are
evaluations of REAL collected data by the alert engine; nothing is predicted.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from backend.common.timeutil import utc_iso

PREFS_DDL = """CREATE TABLE IF NOT EXISTS alert_thresholds (
    user_id TEXT NOT NULL,
    location_id INTEGER,
    label TEXT,
    radius_km REAL DEFAULT 100,
    hazard_types TEXT,              -- JSON array e.g. ["EARTHQUAKE","TSUNAMI","WEATHER"]
    min_severity TEXT DEFAULT 'LOW',-- v2 scale gate: INFORMATION|LOW|MODERATE|HIGH|SEVERE|LIFE_SAFETY
    temp_high_c REAL, temp_low_c REAL,
    rain_mm_h REAL, wind_kmh REAL, aqi REAL,
    quake_mag REAL,
    silent_start TEXT,              -- "HH:MM" local, NULL = no silent hours
    silent_end TEXT,
    silent_days TEXT,               -- JSON array of weekday ints 0=Mon..6=Sun, NULL = all
    silent_tz TEXT,                 -- IANA tz name, NULL = UTC
    silent_override_life_safety INTEGER NOT NULL DEFAULT 1,
    enabled INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, location_id)
);
CREATE TABLE IF NOT EXISTS travel_mode (
    user_id TEXT PRIMARY KEY,
    destination_name TEXT NOT NULL,
    latitude REAL NOT NULL, longitude REAL NOT NULL,
    start_date TEXT NOT NULL,       -- YYYY-MM-DD
    end_date TEXT NOT NULL,
    created_at TEXT NOT NULL
);"""

PREF_FIELDS = (
    "location_id", "label", "radius_km", "hazard_types", "min_severity",
    "temp_high_c", "temp_low_c", "rain_mm_h", "wind_kmh", "aqi", "quake_mag",
    "silent_start", "silent_end", "silent_days", "silent_tz",
    "silent_override_life_safety", "enabled",
)


def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(PREFS_DDL)
    conn.commit()


def get_prefs(conn: sqlite3.Connection, user_id: str = "local",
              location_id: Optional[int] = None) -> List[Dict[str, Any]]:
    ensure_tables(conn)
    sql = "SELECT * FROM alert_thresholds WHERE user_id = ?"
    params: list = [user_id]
    if location_id is not None:
        sql += " AND location_id = ?"
        params.append(location_id)
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def set_prefs(conn: sqlite3.Connection, user_id: str = "local", **fields) -> Dict[str, Any]:
    """Upsert one preference row (location_id None = global/default row)."""
    ensure_tables(conn)
    cols = [f for f in PREF_FIELDS if f in fields]
    cols = cols or ["enabled"]
    now = utc_iso()
    params = [fields.get(c) for c in cols]
    placeholders = ",".join("?" for c in cols)
    updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != "location_id")
    conn.execute(
        f"""INSERT INTO alert_thresholds (user_id, {",".join(cols)}, updated_at)
            VALUES (?, {placeholders}, ?)
            ON CONFLICT(user_id, location_id) DO UPDATE SET
              {updates + "," if updates else ""} updated_at=excluded.updated_at""",
        [user_id, *params, now])
    conn.commit()
    return get_prefs(conn, user_id, fields.get("location_id"))[0]


def _hhmm_to_min(s: Optional[str]) -> Optional[int]:
    try:
        h, m = (s or "").strip().split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return None


def in_silent_hours(prefs: Dict[str, Any],
                    at: Optional[datetime] = None) -> bool:
    """True if `at` (default now, UTC) falls inside the user's silent hours.

    Handles overnight windows (22:00-06:00) and optional weekday filters.
    No silent_start/silent_end configured -> False."""
    start = _hhmm_to_min(prefs.get("silent_start"))
    end = _hhmm_to_min(prefs.get("silent_end"))
    if start is None or end is None:
        return False
    at = at or datetime.now(timezone.utc)
    tzname = prefs.get("silent_tz")
    try:
        from zoneinfo import ZoneInfo
        local = at.astimezone(ZoneInfo(tzname)) if tzname else at
    except Exception:
        local = at
    days = prefs.get("silent_days")
    if days:
        try:
            allowed = json.loads(days) if isinstance(days, str) else days
            if local.weekday() not in [int(d) for d in allowed]:
                return False
        except Exception:
            pass
    cur = local.hour * 60 + local.minute
    if start <= end:
        return start <= cur < end
    return cur >= start or cur < end


def may_notify(severity: Optional[str], prefs: Optional[Dict[str, Any]],
               at: Optional[datetime] = None) -> bool:
    """Notification gate: silent hours suppress everything EXCEPT life-safety
    alerts when the override is enabled (default ON, spec §16)."""
    from backend.alerting.severity import is_life_safety
    if not prefs or not in_silent_hours(prefs, at):
        return True
    if is_life_safety(severity) and prefs.get("silent_override_life_safety", 1):
        return True
    return False


# ---- travel mode ----
def get_travel(conn: sqlite3.Connection, user_id: str = "local") -> Optional[Dict[str, Any]]:
    ensure_tables(conn)
    row = conn.execute("SELECT * FROM travel_mode WHERE user_id = ?",
                       (user_id,)).fetchone()
    return dict(row) if row else None


def set_travel(conn: sqlite3.Connection, user_id: str = "local",
               destination_name: str = "", latitude: float = 0.0,
               longitude: float = 0.0, start_date: str = "",
               end_date: str = "") -> Dict[str, Any]:
    ensure_tables(conn)
    conn.execute(
        """INSERT INTO travel_mode
           (user_id, destination_name, latitude, longitude, start_date, end_date, created_at)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(user_id) DO UPDATE SET
             destination_name=excluded.destination_name, latitude=excluded.latitude,
             longitude=excluded.longitude, start_date=excluded.start_date,
             end_date=excluded.end_date""",
        (user_id, destination_name, latitude, longitude, start_date, end_date,
         utc_iso()))
    conn.commit()
    return get_travel(conn, user_id) or {}


def clear_travel(conn: sqlite3.Connection, user_id: str = "local") -> None:
    ensure_tables(conn)
    conn.execute("DELETE FROM travel_mode WHERE user_id = ?", (user_id,))
    conn.commit()


def travel_active(travel: Optional[Dict[str, Any]],
                 at: Optional[datetime] = None) -> bool:
    if not travel:
        return False
    today = (at or datetime.now(timezone.utc)).date().isoformat()
    return str(travel.get("start_date", "")) <= today <= str(travel.get("end_date", ""))
