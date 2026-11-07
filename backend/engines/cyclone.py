"""Cyclone engine (spec §27) — honest structure, no invented tracks.

Until a real official cyclone source is wired, this engine reports exactly
that: "no active cyclone data source configured". It surfaces any cyclone
events already present in earth_events (from real pipelines) but never
invents tracks, positions, or intensities.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional

from backend.common.timeutil import utc_iso


def status(conn: sqlite3.Connection, days: int = 14) -> Dict[str, Any]:
    """Current cyclone picture: real events if present, honest empty otherwise."""
    rows = conn.execute(
        """SELECT * FROM earth_events WHERE event_type = 'CYCLONE'
           AND start_time >= strftime('%Y-%m-%dT%H:%M:%SZ','now', ?) ORDER BY start_time DESC""",
        (f"-{days} days",)).fetchall()
    events = [dict(r) for r in rows]
    check = conn.execute(
        """SELECT MAX(checked_at) AS t FROM system_health
           WHERE component LIKE 'collector:%cyclone%' OR component LIKE '%cyclone%'""").fetchone()
    verified = check["t"] if check and check["t"] else None
    if not events:
        return {
            "status": "no_active_cyclone_data_source_configured",
            "kind": "INFORMATION",
            "message": ("No cyclone data source is configured and no cyclone events are "
                        "recorded. This is an honest empty state — not a claim that no "
                        "cyclone exists anywhere."),
            "events": [],
            "tracks": [],
            "provenance": {"sources": [], "updated_at": verified},
        }
    return {
        "status": "events_present",
        "kind": "INFORMATION",
        "message": (f"{len(events)} cyclone event(s) in the last {days} days from "
                    "recorded sources. Tracks are shown only when a source provides them."),
        "events": events,
        "tracks": [],  # populated only from real track sources, never synthesized
        "provenance": {"sources": list({e.get("source") for e in events if e.get("source")}),
                       "updated_at": verified},
    }
