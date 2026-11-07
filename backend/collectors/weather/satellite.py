"""Satellite imagery collector — HONEST STUB.

No free, legal satellite imagery tile source is wired yet. This module implements
the collector interface but reports honest unavailability; it writes no pixels
and fabricates nothing. The API must return an honest 'unavailable' state for
/v1/weather/map until a real source is integrated.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict

from backend.common.logging_setup import setup_logging
from backend.common.timeutil import utc_iso

log = setup_logging("aether.collectors.weather.satellite")

MESSAGE = ("No free legal satellite imagery source wired yet. "
           "API returns an honest 'unavailable' state for satellite data.")


def collect(conn: sqlite3.Connection, **kwargs) -> Dict[str, Any]:
    conn.execute(
        """INSERT INTO system_health (component, status, message, checked_at)
           VALUES (?,?,?,?)""",
        ("collector:satellite", "not_configured", MESSAGE, utc_iso()))
    conn.execute(
        "UPDATE source_registry SET health='unavailable', last_failure=? WHERE source_id='satellite-imagery'",
        (utc_iso(),))
    conn.commit()
    log.warning("satellite: %s", MESSAGE)
    return {"ok": False, "status": "unavailable", "records": 0, "message": MESSAGE,
            "last_verified": utc_iso()}
