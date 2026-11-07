"""Tsunami observations (sea-level / tide gauges) — HONEST STUB.

No free, legal sea-level observation feed (e.g. IOC tide gauges) is wired yet.
Implements the collector interface, reports honest unavailability, stores
nothing. Tsunami detection in AETHER today comes from USGS tsunami_flag on
earthquake events + official NWS CAP messages only.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict

from backend.common.logging_setup import setup_logging
from backend.common.timeutil import utc_iso

log = setup_logging("aether.collectors.tsunami.observations")

MESSAGE = ("No free legal sea-level/tide-gauge observation feed wired yet. "
           "Tsunami state comes from USGS tsunami_flag on earthquake events and "
           "official NWS CAP messages only.")


def collect(conn: sqlite3.Connection, **kwargs) -> Dict[str, Any]:
    conn.execute(
        """INSERT INTO system_health (component, status, message, checked_at)
           VALUES (?,?,?,?)""",
        ("collector:tsunami-observations", "not_configured", MESSAGE, utc_iso()))
    conn.execute(
        "UPDATE source_registry SET health='unavailable', last_failure=? WHERE source_id='tsunami-sea-level'",
        (utc_iso(),))
    conn.commit()
    log.warning("tsunami observations: %s", MESSAGE)
    return {"ok": False, "status": "unavailable", "records": 0, "message": MESSAGE,
            "last_verified": utc_iso()}
