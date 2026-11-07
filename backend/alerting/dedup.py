"""Alert dedup keys: stable, deterministic fingerprints so the same hazard
does not produce duplicate active alerts.

v2 (spec §13): alerts merge on event ID + location + time window + hazard
type + source event ID + severity. A magnitude revision ("M6.2 -> M6.4 -> M6.5")
is ONE event with revisions; the merge key deliberately EXCLUDES severity so
revisions collide onto the same alert, and notification escalation is decided
by a meaningful severity change (see severity.is_meaningful_escalation).
"""
from __future__ import annotations

import hashlib
from typing import Optional


def make_dedup_key(source: str, type_: str, severity: str,
                   event_id: Optional[str] = None,
                   location_id: Optional[int] = None,
                   window: Optional[str] = None) -> str:
    """Legacy dedup key (kept byte-compatible). Includes severity."""
    parts = [source, type_, severity, event_id or "-", str(location_id or "-"),
             window or "-"]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]


def event_merge_key(source: str, type_: str,
                    event_id: Optional[str] = None,
                    location_id: Optional[int] = None,
                    window: Optional[str] = None) -> str:
    """Severity-independent merge key: revisions of the same event (M6.2 ->
    M6.4 -> M6.5) produce the same key and merge into one alert with
    revisions instead of separate alerts."""
    parts = [source, type_, event_id or "-", str(location_id or "-"),
             window or "-"]
    return "evt:" + hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]


def same_event(a: dict, b: dict) -> bool:
    """True when two alert dicts describe the same hazard event:
    event ID + hazard type + source must match; location and a 24h window
    guard against re-using an event ID for a different occurrence."""
    if not a.get("event_id") or a.get("event_id") != b.get("event_id"):
        return False
    if (a.get("type") or "").upper() != (b.get("type") or "").upper():
        return False
    if (a.get("source") or "") != (b.get("source") or ""):
        return False
    la, lb = a.get("location_id"), b.get("location_id")
    if la is not None and lb is not None and la != lb:
        return False
    return True
