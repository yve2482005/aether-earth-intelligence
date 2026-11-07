"""Coastal tsunami risk scoring — heuristic RISK assessment, labeled as such.

Scoring rule (documented, honest):
- Any ACTIVE official tsunami warning/advisory/watch message => CRITICAL
  (priority override; official messages always win).
- Else, from recent earthquake activity near the coast point:
    M>=7.5 within 2000 km (24h) -> HIGH
    M>=6.5 within 500 km (24h)  -> MODERATE
    otherwise                  -> LOW
This is a heuristic screening score (kind=RISK), NOT an official warning and
NOT a prediction. Inputs are listed in the output for transparency.
"""
from __future__ import annotations

import math
import sqlite3
from typing import Any, Dict, List

from backend.common.timeutil import utc_iso
from backend.collectors.earthquake.event_deduplicator import haversine_km

LEVELS = ("LOW", "MODERATE", "HIGH", "CRITICAL")

DISCLAIMER = ("Heuristic screening score only (kind=RISK). Not an official tsunami "
              "warning and not a prediction. Follow official warnings from your "
              "national tsunami warning center.")


def compute_coastal_risk(conn: sqlite3.Connection, lat: float, lon: float,
                         place_name: str = "") -> Dict[str, Any]:
    inputs: List[str] = []
    # Priority override: any active official warning message.
    active = conn.execute(
        """SELECT tsunami_event_id, message_type, affected_region, issue_time
           FROM tsunami_events WHERE status = 'active'
           ORDER BY issue_time DESC LIMIT 5""").fetchall()
    if active:
        return {
            "level": "CRITICAL",
            "kind": "RISK",
            "reason": "Active official tsunami message(s) present — priority override.",
            "official_messages": [dict(r) for r in active],
            "inputs": inputs,
            "disclaimer": DISCLAIMER,
            "assessed_at": utc_iso(),
        }
    # Heuristic from recent seismicity.
    rows = conn.execute(
        """SELECT canonical_event_id, magnitude, region, origin_time, latitude, longitude
           FROM earthquake_events
           WHERE origin_time >= strftime('%Y-%m-%dT%H:%M:%SZ','now', '-1 day') AND magnitude IS NOT NULL
           ORDER BY magnitude DESC LIMIT 200""").fetchall()
    level = "LOW"
    contributors: List[Dict[str, Any]] = []
    for r in rows:
        d = haversine_km(lat, lon, r["latitude"], r["longitude"])
        m = r["magnitude"]
        tag = None
        if m >= 7.5 and d <= 2000:
            tag, level = "M>=7.5 within 2000km", "HIGH"
        elif m >= 6.5 and d <= 500 and level in ("LOW",):
            tag, level = "M>=6.5 within 500km", "MODERATE"
        if tag:
            contributors.append({**dict(r), "distance_km": round(d, 1), "rule": tag})
            inputs.append(f"{tag}: {r['canonical_event_id']} M{m} @ {round(d)}km")
    return {
        "level": level,
        "kind": "RISK",
        "reason": ("No active official tsunami messages. Score derived from "
                   "earthquakes in the last 24h near the point."),
        "contributors": contributors[:5],
        "inputs": inputs,
        "disclaimer": DISCLAIMER,
        "assessed_at": utc_iso(),
    }
