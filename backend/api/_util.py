"""Shared API helpers: provenance envelopes, location resolution."""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, Query

from backend.common.timeutil import age_minutes, utc_iso
from backend.db import db as dbmod


def provenance(sources: List[str], updated_at: Optional[str],
               extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    prov = {
        "sources": sources,
        "updated_at": updated_at,
        "data_age_min": round(age_minutes(updated_at), 1) if age_minutes(updated_at) is not None else None,
    }
    if extra:
        prov.update(extra)
    return prov


def resolve_location(conn: sqlite3.Connection, lat: Optional[float], lon: Optional[float],
                     location_id: Optional[int] = None) -> Dict[str, Any]:
    """Resolve a location by id or lat/lon (creating a row if needed)."""
    if location_id is not None:
        row = conn.execute("SELECT * FROM locations WHERE id = ?", (location_id,)).fetchone()
        if row is None:
            raise HTTPException(404, f"location_id {location_id} not found")
        return dict(row)
    if lat is None or lon is None:
        raise HTTPException(400, "supply lat & lon, or location_id")
    row = conn.execute(
        """SELECT * FROM locations WHERE ABS(latitude - ?) < 0.05 AND ABS(longitude - ?) < 0.05
           ORDER BY id LIMIT 1""", (lat, lon)).fetchone()
    if row:
        return dict(row)
    cur = conn.execute(
        """INSERT INTO locations (name, latitude, longitude, timezone, country, is_coastal, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (f"{lat:.2f},{lon:.2f}", lat, lon, "UTC", None, 0, utc_iso()))
    conn.commit()
    return dict(conn.execute("SELECT * FROM locations WHERE id = ?",
                             (cur.lastrowid,)).fetchone())


def unavailable(message: str, sources: List[str], last_verified: Optional[str],
                kind: str = "OBSERVATION") -> Dict[str, Any]:
    return {
        "status": "unavailable",
        "kind": kind,
        "message": message,
        "provenance": provenance(sources, last_verified),
    }


CONFIDENCE_PERCENT = {"HIGH": 85, "MEDIUM": 65, "LOW": 40}


def confidence_payload(kind: str, **kwargs) -> Dict[str, Any]:
    """assess() output plus UI-friendly aliases: percent + reasons (same as why).

    The web UI reads conf.percent / conf.reasons; the canonical keys
    (level/why) are preserved untouched.
    """
    from backend.confidence.engine import assess
    conf = assess(kind, **kwargs)
    conf["percent"] = CONFIDENCE_PERCENT.get(conf["level"], 40)
    conf["reasons"] = list(conf.get("why", []))
    return conf
