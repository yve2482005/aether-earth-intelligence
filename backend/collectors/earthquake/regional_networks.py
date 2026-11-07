"""Regional seismic networks — HONEST STUB.

No regional network feed (e.g. national observatories beyond USGS) is wired
yet. Interface is ready so a future source can plug in through the normalizer /
deduplicator / tracker pipeline (spatial+temporal merge). Until then this
reports honest unavailability.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict

from backend.common.logging_setup import setup_logging
from backend.common.timeutil import utc_iso

log = setup_logging("aether.collectors.earthquake.regional_networks")

MESSAGE = ("No regional seismic network source wired yet. "
           "USGS remains the only live earthquake source.")


def collect(conn: sqlite3.Connection, **kwargs) -> Dict[str, Any]:
    conn.execute(
        """INSERT INTO system_health (component, status, message, checked_at)
           VALUES (?,?,?,?)""",
        ("collector:regional-seismic-networks", "not_configured", MESSAGE, utc_iso()))
    conn.execute(
        "UPDATE source_registry SET health='unavailable', last_failure=? WHERE source_id='regional-seismic-networks'",
        (utc_iso(),))
    conn.commit()
    log.warning("regional_networks: %s", MESSAGE)
    return {"ok": False, "status": "unavailable", "records": 0, "message": MESSAGE,
            "last_verified": utc_iso()}
