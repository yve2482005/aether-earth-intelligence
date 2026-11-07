"""Event deduplicator.

- USGS events: canonical_event_id is the authoritative USGS 'id' (unique, stable).
  source_event_ids (JSON) accumulates aliases (ids string from feed).
- Non-USGS sources (regional networks, future): spatial+temporal proximity merge
  against existing events (±120 s, 150 km) returns a merge candidate id.
"""
from __future__ import annotations

import json
import math
import sqlite3
from typing import Any, Dict, List, Optional

from backend.common.timeutil import parse_iso

MERGE_WINDOW_SECONDS = 120
MERGE_RADIUS_KM = 150.0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def canonical_id_for_usgs(record: Dict[str, Any]) -> str:
    return record["canonical_event_id"]


def find_merge_candidate(conn: sqlite3.Connection, lat: float, lon: float,
                         origin_time: str, source: str) -> Optional[str]:
    """Return canonical_event_id of an existing event that likely matches a non-USGS report."""
    t0 = parse_iso(origin_time)
    if t0 is None or lat is None or lon is None:
        return None
    rows = conn.execute(
        """SELECT canonical_event_id, latitude, longitude, origin_time FROM earthquake_events
           WHERE source != ?""", (source,)).fetchall()
    best: Optional[str] = None
    best_dist = MERGE_RADIUS_KM
    for r in rows:
        t1 = parse_iso(r["origin_time"])
        if t1 is None or abs((t0 - t1).total_seconds()) > MERGE_WINDOW_SECONDS:
            continue
        d = haversine_km(lat, lon, r["latitude"], r["longitude"])
        if d < best_dist:
            best_dist, best = d, r["canonical_event_id"]
    return best


def merge_aliases(existing_json: str, new_ids: List[str]) -> str:
    try:
        cur = json.loads(existing_json) if existing_json else []
    except (ValueError, TypeError):
        cur = []
    for i in new_ids:
        if i not in cur:
            cur.append(i)
    return json.dumps(cur)
