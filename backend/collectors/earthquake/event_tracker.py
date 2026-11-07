"""Event tracker: upsert earthquake_events + append-only revision log.

Provenance is never overwritten: every update appends a row to earthquake_updates
recording the values seen in this poll. The current row in earthquake_events is
always the latest reported values.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict

from backend.common.timeutil import utc_iso
from backend.collectors.earthquake.event_deduplicator import merge_aliases
from backend.processing.validation import validate_earthquake_row

COLUMNS = ["canonical_event_id", "source", "source_event_ids", "origin_time", "latitude",
           "longitude", "depth", "magnitude", "magnitude_type", "region", "status",
           "review_status", "felt_count", "mmi", "alert_level", "tsunami_flag",
           "shakemap_available", "pager_available", "source_url", "update_time"]


def upsert(conn: sqlite3.Connection, record: Dict[str, Any]) -> Dict[str, Any]:
    """Upsert one normalized record. Returns {action: inserted|updated|invalid, ...}."""
    clean, errors = validate_earthquake_row(record)
    if not clean.get("canonical_event_id") or "invalid:coordinates-missing" in errors \
            or "missing:origin_time" in errors:
        return {"action": "invalid", "errors": errors}
    eid = clean["canonical_event_id"]
    existing = conn.execute(
        "SELECT * FROM earthquake_events WHERE canonical_event_id = ?", (eid,)).fetchone()
    ids = json.loads(record["source_event_ids"]) if record.get("source_event_ids") else []
    if existing is None:
        conn.execute(
            f"""INSERT INTO earthquake_events ({','.join(COLUMNS)}, created_at)
                VALUES ({','.join('?' * len(COLUMNS))}, ?)""",
            tuple(record.get(c) for c in COLUMNS) + (utc_iso(),))
        action = "inserted"
    else:
        merged_ids = merge_aliases(existing["source_event_ids"], ids)
        updates = {c: record.get(c) for c in COLUMNS if c != "canonical_event_id"}
        updates["source_event_ids"] = merged_ids
        set_clause = ", ".join(f"{c} = ?" for c in updates)
        conn.execute(f"UPDATE earthquake_events SET {set_clause} WHERE canonical_event_id = ?",
                     tuple(updates.values()) + (eid,))
        action = "updated"
    # Append-only revision log (provenance).
    conn.execute(
        """INSERT INTO earthquake_updates
           (canonical_event_id, source_event_id, origin_time, latitude, longitude, depth,
            magnitude, magnitude_type, status, update_time, received_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (eid, ids[0] if ids else None, record.get("origin_time"), record.get("latitude"),
         record.get("longitude"), record.get("depth"), record.get("magnitude"),
         record.get("magnitude_type"), record.get("status"), record.get("update_time"),
         utc_iso()))
    conn.commit()
    return {"action": action, "canonical_event_id": eid}
