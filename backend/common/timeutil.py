"""UTC ISO-8601 time helpers. All timestamps stored as UTC ISO-8601 strings."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Optional


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(dt: Optional[datetime] = None) -> str:
    """Return UTC ISO-8601 with trailing 'Z'."""
    dt = dt or now_utc()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(s: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 string (accepts trailing 'Z'). Returns aware datetime or None."""
    if not s:
        return None
    try:
        t = s.strip()
        if t.endswith("Z"):
            t = t[:-1] + "+00:00"
        dt = datetime.fromisoformat(t)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def age_minutes(ts: Optional[str], ref: Optional[datetime] = None) -> Optional[float]:
    """Minutes between ts and ref (default now). None if unparseable."""
    dt = parse_iso(ts)
    if dt is None:
        return None
    ref = ref or now_utc()
    return (ref - dt).total_seconds() / 60.0


def hour_floor(dt: Optional[datetime] = None) -> datetime:
    dt = dt or now_utc()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def is_fresh(ts: Optional[str], max_age_min: float) -> bool:
    age = age_minutes(ts)
    return age is not None and age <= max_age_min


def sql_now_iso(*modifiers: str) -> str:
    """SQLite expression for "now" in the ISO-8601 'Z' text format the DB stores.

    Plain datetime('now') yields 'YYYY-MM-DD HH:MM:SS', which sorts AFTER
    'YYYY-MM-DDT...Z' strings and breaks upper-bound BETWEEN checks.
    Modifiers: sql_now_iso('-2 hours') ->
      strftime('%Y-%m-%dT%H:%M:%SZ','now','-2 hours')
    """
    args = ",".join(["'%Y-%m-%dT%H:%M:%SZ'", "'now'"] +
                    ["'%s'" % m for m in modifiers])
    return "strftime(%s)" % args
