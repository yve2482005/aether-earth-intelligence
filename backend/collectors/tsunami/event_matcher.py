"""Tsunami <-> earthquake event matcher.

Links a tsunami message to potentially relevant earthquake events by time and
space. Correlation language only: "potentially relevant earthquake detected".
NEVER "tsunami will occur" without an authoritative warning.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional

from backend.common.timeutil import parse_iso

CORRELATION_NOTE = ("Correlation only: a potentially relevant earthquake was detected near "
                    "the message time. This is NOT a tsunami prediction and does not "
                    "replace an official warning.")


def match_message_to_earthquakes(conn: sqlite3.Connection, tsunami_event_id: str,
                                 window_hours: float = 6.0,
                                 max_results: int = 5) -> Dict[str, Any]:
    msg = conn.execute(
        "SELECT * FROM tsunami_events WHERE tsunami_event_id = ?",
        (tsunami_event_id,)).fetchone()
    if msg is None:
        return {"tsunami_event_id": tsunami_event_id, "matches": [],
                "note": "Message not found."}
    msg = dict(msg)
    t0 = parse_iso(msg.get("issue_time"))
    if t0 is None:
        return {"tsunami_event_id": tsunami_event_id, "matches": [],
                "note": "Message has no parseable issue time; cannot correlate."}
    rows = conn.execute(
        """SELECT canonical_event_id, magnitude, region, origin_time, latitude, longitude,
                  tsunami_flag
           FROM earthquake_events
           ORDER BY origin_time DESC LIMIT 500""").fetchall()
    matches: List[Dict[str, Any]] = []
    for r in rows:
        et = parse_iso(r["origin_time"])
        if et is None:
            continue
        gap_h = abs((t0 - et).total_seconds()) / 3600.0
        if gap_h <= window_hours and (r["tsunami_flag"] or (r["magnitude"] or 0) >= 6.5):
            matches.append({**dict(r), "gap_hours": round(gap_h, 2)})
        if len(matches) >= max_results:
            break
    matches.sort(key=lambda m: (not m["tsunami_flag"], m["gap_hours"]))
    return {"tsunami_event_id": tsunami_event_id, "matches": matches,
            "note": CORRELATION_NOTE, "kind": "DETECTION"}


def link(conn: sqlite3.Connection, tsunami_event_id: str,
         canonical_event_id: str) -> None:
    conn.execute(
        "UPDATE tsunami_events SET earthquake_event_id = ? WHERE tsunami_event_id = ?",
        (canonical_event_id, tsunami_event_id))
    conn.commit()
