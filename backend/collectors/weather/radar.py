"""Weather radar collector — HONEST STUB.

No free, legal radar tile source is wired yet. Implements the collector interface,
reports honest unavailability, fabricates nothing.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict

from backend.common.logging_setup import setup_logging
from backend.common.timeutil import utc_iso

log = setup_logging("aether.collectors.weather.radar")

MESSAGE = ("No free legal radar tile source wired yet. "
           "API returns an honest 'unavailable' state for radar data.")


def collect(conn: sqlite3.Connection, **kwargs) -> Dict[str, Any]:
    conn.execute(
        """INSERT INTO system_health (component, status, message, checked_at)
           VALUES (?,?,?,?)""",
        ("collector:radar", "not_configured", MESSAGE, utc_iso()))
    conn.execute(
        "UPDATE source_registry SET health='unavailable', last_failure=? WHERE source_id='radar-imagery'",
        (utc_iso(),))
    conn.commit()
    log.warning("radar: %s", MESSAGE)
    return {"ok": False, "status": "unavailable", "records": 0, "message": MESSAGE,
            "last_verified": utc_iso()}
