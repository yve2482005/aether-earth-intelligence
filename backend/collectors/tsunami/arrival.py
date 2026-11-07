"""Tsunami arrival estimates — official text only.

Arrival estimates are extracted ONLY from official message text. When no
official estimate exists, the honest answer is exactly:
"Arrival estimate unavailable."
"""
from __future__ import annotations

import re
import sqlite3
from typing import Any, Dict, Optional

UNAVAILABLE = "Arrival estimate unavailable."


def extract_arrival(raw_text: Optional[str]) -> Dict[str, Any]:
    if not raw_text:
        return {"available": False, "estimate": UNAVAILABLE, "kind": "WARNING"}
    # Look for explicit estimated-arrival statements in official text.
    patterns = [
        r"estimated arrival[^.\n]{0,120}",
        r"arrival[^.\n]{0,80}estimated[^.\n]{0,80}",
        r"waves? expected[^.\n]{0,120}",
    ]
    for p in patterns:
        m = re.search(p, raw_text, re.IGNORECASE)
        if m:
            return {"available": True, "estimate": m.group(0).strip(),
                    "kind": "WARNING",
                    "note": "Verbatim from official message; not computed by AETHER."}
    return {"available": False, "estimate": UNAVAILABLE, "kind": "WARNING"}


def arrival_for_event(conn: sqlite3.Connection, tsunami_event_id: str) -> Dict[str, Any]:
    row = conn.execute(
        "SELECT arrival_time, raw_classification FROM tsunami_events WHERE tsunami_event_id = ?",
        (tsunami_event_id,)).fetchone()
    if row is None:
        return {"available": False, "estimate": UNAVAILABLE, "kind": "WARNING",
                "note": "Tsunami message not found."}
    msg = conn.execute(
        "SELECT raw_text FROM tsunami_messages WHERE tsunami_event_id = ? ORDER BY id DESC LIMIT 1",
        (tsunami_event_id,)).fetchone()
    raw = (msg["raw_text"] if msg else None) or row["arrival_time"]
    return extract_arrival(raw)
