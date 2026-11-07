"""Felt reports (DYFI summary).

The summary feed carries only aggregate felt counts (properties['felt']) and
max reported intensity (properties['cdi']). Full DYFI city-level responses
require the ComCat detail document. This module extracts and stores the
summary-level values honestly.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional


def summary_from_event_row(row: Dict[str, Any]) -> Dict[str, Any]:
    felt = row.get("felt_count")
    return {
        "canonical_event_id": row.get("canonical_event_id"),
        "felt_count": felt,
        "max_reported_intensity": row.get("mmi"),
        "has_reports": bool(felt),
        "kind": "OBSERVATION",
        "note": ("Summary-level only (USGS feed aggregates). Full DYFI city-level "
                 "responses require the ComCat detail document for this event."),
    }


def top_felt_events(conn: sqlite3.Connection, limit: int = 10,
                    since: Optional[str] = None) -> List[Dict[str, Any]]:
    sql = ("SELECT canonical_event_id, felt_count, mmi, magnitude, region, origin_time "
           "FROM earthquake_events WHERE felt_count IS NOT NULL")
    params: list = []
    if since:
        sql += " AND origin_time >= ?"
        params.append(since)
    sql += " ORDER BY felt_count DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(sql, params).fetchall()]
