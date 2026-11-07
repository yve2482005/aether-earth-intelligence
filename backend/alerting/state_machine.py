"""Alert state machine (spec §12).

States:
  DETECTED -> VALIDATING -> CONFIRMED -> SEVERITY_ASSESSED -> ALERTED
  ALERTED -> UPDATED (revisions) | RESOLVED
  UPDATED -> UPDATED | RESOLVED
  RESOLVED -> ARCHIVED ; ARCHIVED is terminal.

Every transition is recorded in alert_revisions. Illegal transitions raise
InvalidTransition instead of silently corrupting state.

The engine bootstraps every newly-issued alert at DETECTED and drives it
through VALIDATING -> CONFIRMED -> SEVERITY_ASSESSED immediately (the engine
itself performs detection/validation/severity assessment). Notification
dispatch (FCM etc.) is a separate concern handled by backend.notify.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional

from backend.common.timeutil import utc_iso

STATES = (
    "DETECTED",
    "VALIDATING",
    "CONFIRMED",
    "SEVERITY_ASSESSED",
    "ALERTED",
    "UPDATED",
    "RESOLVED",
    "ARCHIVED",
)

# from_state -> allowed to_states
ALLOWED = {
    "DETECTED": ("VALIDATING", "RESOLVED", "ARCHIVED"),
    "VALIDATING": ("CONFIRMED", "RESOLVED", "ARCHIVED"),
    "CONFIRMED": ("SEVERITY_ASSESSED", "RESOLVED", "ARCHIVED"),
    "SEVERITY_ASSESSED": ("ALERTED", "UPDATED", "RESOLVED", "ARCHIVED"),
    "ALERTED": ("UPDATED", "RESOLVED", "ARCHIVED"),
    "UPDATED": ("UPDATED", "RESOLVED", "ARCHIVED"),
    "RESOLVED": ("ARCHIVED",),
    "ARCHIVED": (),
}

REVISIONS_DDL = """CREATE TABLE IF NOT EXISTS alert_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id TEXT NOT NULL,
    from_state TEXT NOT NULL,
    to_state TEXT NOT NULL,
    changed_at TEXT NOT NULL,
    reason TEXT,
    actor TEXT,
    version INTEGER
);
CREATE INDEX IF NOT EXISTS idx_revisions_alert ON alert_revisions(alert_id, changed_at);"""

# Fallback name used only when another v2 workstream's alert_revisions table
# (shape: revision/title/message/severity/status/changed_by) already exists in
# the DB. Both shapes serve different concerns; the fallback keeps this module
# additive and non-breaking either way. See docs/NOTIFICATIONS.md "schema notes".
REVISIONS_FALLBACK = "alert_state_revisions"
_REVISION_COLS = ("alert_id", "from_state", "to_state", "changed_at",
                  "reason", "actor", "version")


def _revision_table(conn: sqlite3.Connection) -> str:
    """Resolve the revision table name, creating it if needed.

    Prefers `alert_revisions` (task-specified name/columns). If that table
    already exists with an incompatible shape (another workstream's DDL),
    falls back to `alert_state_revisions` with our columns.
    """
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='alert_revisions'"
    ).fetchone()
    if row is None:
        conn.executescript(REVISIONS_DDL)
        conn.commit()
        return "alert_revisions"
    cols = {r[1] for r in conn.execute("PRAGMA table_info(alert_revisions)").fetchall()}
    if set(_REVISION_COLS) <= cols:
        return "alert_revisions"
    conn.executescript(REVISIONS_DDL.replace("alert_revisions", REVISIONS_FALLBACK))
    conn.commit()
    return REVISIONS_FALLBACK

# New per-alert metadata columns added idempotently to the existing alerts table.
_ALERT_COLUMNS = {
    "version": "INTEGER NOT NULL DEFAULT 1",
    "state": "TEXT NOT NULL DEFAULT 'SEVERITY_ASSESSED'",
    "confidence": "TEXT",                                  # high|medium|low
    "notification_status": "TEXT NOT NULL DEFAULT 'pending'",
    # pending | dispatched | delivered | failed | not_configured |
    # escalation_pending | suppressed_silent
}


class InvalidTransition(Exception):
    """Raised when an alert state transition is not allowed."""


def ensure_tables(conn: sqlite3.Connection) -> None:
    """Create the revision table (adaptive name) and add new alerts columns if missing."""
    _revision_table(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(alerts)").fetchall()}
    for name, ddl in _ALERT_COLUMNS.items():
        if name not in cols:
            conn.execute(f"ALTER TABLE alerts ADD COLUMN {name} {ddl}")
    conn.commit()


def current_state(conn: sqlite3.Connection, alert_id: str) -> Optional[str]:
    row = conn.execute("SELECT state FROM alerts WHERE alert_id = ?",
                       (alert_id,)).fetchone()
    return row["state"] if row and row["state"] else None


def transition(conn: sqlite3.Connection, alert_id: str, to_state: str,
               reason: Optional[str] = None, actor: str = "alert-engine",
               bump_version: bool = False) -> str:
    """Move alert_id to to_state. Raises InvalidTransition if not allowed."""
    if to_state not in STATES:
        raise InvalidTransition(f"unknown state {to_state!r}")
    row = conn.execute(
        "SELECT state, version FROM alerts WHERE alert_id = ?", (alert_id,)).fetchone()
    if row is None:
        raise InvalidTransition(f"alert {alert_id!r} does not exist")
    from_state = row["state"] or "DETECTED"
    if to_state not in ALLOWED.get(from_state, ()):
        raise InvalidTransition(
            f"illegal transition {from_state} -> {to_state} for alert {alert_id!r}")
    new_version = (row["version"] or 1) + (1 if bump_version else 0)
    conn.execute(
        "UPDATE alerts SET state = ?, version = ? WHERE alert_id = ?",
        (to_state, new_version, alert_id))
    table = _revision_table(conn)
    conn.execute(
        f"""INSERT INTO {table}
           (alert_id, from_state, to_state, changed_at, reason, actor, version)
           VALUES (?,?,?,?,?,?,?)""",
        (alert_id, from_state, to_state, utc_iso(), reason, actor, new_version))
    conn.commit()
    return to_state


def bootstrap(conn: sqlite3.Connection, alert_id: str,
              actor: str = "alert-engine") -> str:
    """Drive a freshly-inserted alert (state DETECTED) through validation to
    SEVERITY_ASSESSED. The engine performs detection/validation/assessment
    synchronously, so the intermediate states are recorded as completed."""
    ensure_tables(conn)
    state = current_state(conn, alert_id)
    if state is None:
        raise InvalidTransition(f"alert {alert_id!r} does not exist")
    # Only bootstrap alerts that have not progressed past DETECTED.
    if state != "DETECTED":
        return state
    transition(conn, alert_id, "VALIDATING", "signal validated against source",
               actor=actor)
    transition(conn, alert_id, "CONFIRMED", "hazard confirmed from source data",
               actor=actor)
    transition(conn, alert_id, "SEVERITY_ASSESSED",
               "severity assessed by alert engine", actor=actor)
    return "SEVERITY_ASSESSED"


def expire(conn: sqlite3.Connection, alert_id: str,
           reason: str = "expired") -> Optional[str]:
    """Move an alert to RESOLVED (terminal-ish; ARCHIVED later)."""
    state = current_state(conn, alert_id)
    if state in (None, "RESOLVED", "ARCHIVED"):
        return state
    return transition(conn, alert_id, "RESOLVED", reason, actor="alert-engine")


def archive_old(conn: sqlite3.Connection, days: int = 30) -> int:
    """RESOLVED alerts older than <days> -> ARCHIVED. Rows are never deleted."""
    cur = conn.execute(
        """SELECT alert_id FROM alerts
           WHERE state = 'RESOLVED'
             AND issued_at < strftime('%Y-%m-%dT%H:%M:%SZ','now', ?)""", (f"-{days} days",))
    ids = [r["alert_id"] for r in cur.fetchall()]
    n = 0
    for aid in ids:
        try:
            transition(conn, aid, "ARCHIVED", f"auto-archive after {days}d",
                       actor="alert-engine")
            n += 1
        except InvalidTransition:
            continue
    return n


def revisions(conn: sqlite3.Connection, alert_id: str) -> List[Dict[str, Any]]:
    table = _revision_table(conn)
    return [dict(r) for r in conn.execute(
        f"""SELECT alert_id, from_state, to_state, changed_at, reason, actor, version
           FROM {table} WHERE alert_id = ? ORDER BY id ASC""",
        (alert_id,)).fetchall()]


def revision_table_name(conn: sqlite3.Connection) -> str:
    """Public accessor for the resolved revision table (API/docs use)."""
    return _revision_table(conn)
