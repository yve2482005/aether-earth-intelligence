"""AETHER v2 — incident tracking (§71) + source failover log (§§35-36) + alert delivery audit (§79).

REUSES the existing v1 tables (created by backend/db/schema.sql / migrate_v2.py):
  incidents(id TEXT PK, title, component, severity, status, started_at, ended_at,
            summary, created_at, updated_at)
  notification_deliveries(id, notification_id -> notification_history.id, channel,
                          provider_message_id, delivered_at, status, detail)
  source_failover(id, category, from_source, to_source, reason, occurred_at)

Plus one NEW additive table owned by this module:
  incident_notes(incident_id, at, event, kind) — kind in created|type|note|resolution.

Additive only: existing tables are never altered, renamed, or dropped.
Incident "type" (source_outage|model_failure|alert_failure|db_failure|other) is
stored as an incident_notes row of kind='type' and mapped to v1 severity.
Delivery rows record ONLY what actually happened — no synthetic entries.
"""
from __future__ import annotations

import random
import sqlite3
from typing import Any, Dict, List, Optional

from backend.common.timeutil import utc_iso

_TYPE_SEVERITY = {
    "source_outage": "major",
    "model_failure": "major",
    "alert_failure": "critical",
    "db_failure": "critical",
    "other": "minor",
}


def ensure_tables(conn: sqlite3.Connection) -> None:
    """Create the v1 tables if missing (no-op on the real DB) plus incident_notes."""
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS incidents (
        incident_id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        component TEXT,
        severity TEXT,
        status TEXT NOT NULL DEFAULT 'open',
        started_at TEXT NOT NULL, ended_at TEXT,
        summary TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_incidents_status ON incidents(status, created_at);
    CREATE TABLE IF NOT EXISTS notification_deliveries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        notification_id INTEGER NOT NULL REFERENCES notification_history(id),
        channel TEXT NOT NULL,
        provider_message_id TEXT,
        delivered_at TEXT,
        status TEXT NOT NULL,
        detail TEXT
    );
    CREATE TABLE IF NOT EXISTS source_failover (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        category TEXT NOT NULL,
        from_source TEXT NOT NULL,
        to_source TEXT NOT NULL,
        reason TEXT,
        occurred_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS notification_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        alert_id TEXT,
        channel TEXT NOT NULL,
        recipient TEXT,
        title TEXT, body TEXT,
        status TEXT NOT NULL DEFAULT 'queued',
        error TEXT,
        created_at TEXT NOT NULL, sent_at TEXT
    );
    CREATE TABLE IF NOT EXISTS incident_notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        incident_id TEXT NOT NULL,
        at TEXT NOT NULL,
        event TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'note'
    );
    CREATE INDEX IF NOT EXISTS idx_incident_notes ON incident_notes(incident_id, at);
    """)
    conn.commit()


def _note_type(conn: sqlite3.Connection, incident_id: str) -> Optional[str]:
    row = conn.execute(
        "SELECT event FROM incident_notes WHERE incident_id = ? AND kind = 'type' "
        "ORDER BY at LIMIT 1", (incident_id,)).fetchone()
    return row["event"] if row else None


def _timeline(conn: sqlite3.Connection, incident_id: str) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT at, event, kind FROM incident_notes WHERE incident_id = ? "
        "ORDER BY at, id", (incident_id,)).fetchall()
    return [{"at": r["at"], "event": r["event"], "kind": r["kind"]} for r in rows]


def _shape(conn: sqlite3.Connection, row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["incident_id"],
        "type": _note_type(conn, row["incident_id"]),
        "component": row["component"],
        "title": row["title"],
        "detail": row["summary"],
        "severity": row["severity"],
        "status": row["status"],
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "timeline": _timeline(conn, row["incident_id"]),
    }


def _new_incident_id(conn: sqlite3.Connection) -> str:
    for _ in range(10):
        iid = "INC-%s-%04d" % (utc_iso().replace("-", "").replace(":", "").replace("T", "")[:14],
                               random.randint(0, 9999))
        if not conn.execute("SELECT 1 FROM incidents WHERE incident_id = ?",
                            (iid,)).fetchone():
            return iid
    raise RuntimeError("could not generate unique incident_id")


def record_incident(conn: sqlite3.Connection, type: str, component: str,
                    title: str, detail: str = "",
                    severity: Optional[str] = None) -> Dict[str, Any]:
    """Open a new incident. severity defaults from type via _TYPE_SEVERITY."""
    ensure_tables(conn)
    now = utc_iso()
    iid = _new_incident_id(conn)
    sev = severity or _TYPE_SEVERITY.get(type, "minor")
    conn.execute(
        """INSERT INTO incidents
           (incident_id, title, component, severity, status,
            started_at, summary, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (iid, title, component, sev, "open", now, detail, now, now))
    conn.execute(
        "INSERT INTO incident_notes (incident_id, at, event, kind) VALUES (?,?,?,?)",
        (iid, now, type, "type"))
    conn.execute(
        "INSERT INTO incident_notes (incident_id, at, event, kind) VALUES (?,?,?,?)",
        (iid, now, "Incident opened: " + title, "created"))
    conn.commit()
    return get_incident(conn, iid)


def get_incident(conn: sqlite3.Connection, incident_id: str) -> Optional[Dict[str, Any]]:
    ensure_tables(conn)
    row = conn.execute("SELECT * FROM incidents WHERE incident_id = ?",
                       (incident_id,)).fetchone()
    return _shape(conn, row) if row else None


def list_incidents(conn: sqlite3.Connection, status: Optional[str] = None,
                   limit: int = 100) -> List[Dict[str, Any]]:
    ensure_tables(conn)
    if status:
        rows = conn.execute(
            "SELECT * FROM incidents WHERE status = ? "
            "ORDER BY started_at DESC LIMIT ?", (status, limit)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM incidents ORDER BY started_at DESC LIMIT ?",
            (limit,)).fetchall()
    return [_shape(conn, r) for r in rows]


def add_timeline_note(conn: sqlite3.Connection, incident_id: str, event: str) -> bool:
    """Append a timeline entry. Returns False if the incident does not exist."""
    ensure_tables(conn)
    if not conn.execute("SELECT 1 FROM incidents WHERE incident_id = ?",
                        (incident_id,)).fetchone():
        return False
    now = utc_iso()
    conn.execute(
        "INSERT INTO incident_notes (incident_id, at, event, kind) VALUES (?,?,?,'note')",
        (incident_id, now, event))
    conn.execute("UPDATE incidents SET updated_at = ? WHERE incident_id = ?",
                 (now, incident_id))
    conn.commit()
    return True


def resolve_incident(conn: sqlite3.Connection, incident_id: str,
                     resolution: str) -> bool:
    """Mark resolved with a resolution note. Returns False if not found."""
    ensure_tables(conn)
    now = utc_iso()
    cur = conn.execute(
        "UPDATE incidents SET status = 'resolved', ended_at = ?, updated_at = ? "
        "WHERE incident_id = ? AND status != 'resolved'",
        (now, now, incident_id))
    if cur.rowcount == 0:
        return False
    conn.execute(
        "INSERT INTO incident_notes (incident_id, at, event, kind) VALUES (?,?,?,'resolution')",
        (incident_id, now, resolution))
    conn.commit()
    return True


_V1_STATUSES = ("open", "investigating", "mitigated", "resolved", "closed")


def set_status(conn: sqlite3.Connection, incident_id: str, status: str) -> Dict[str, Any]:
    """Change status to a v1 lifecycle value. Raises KeyError/ValueError."""
    ensure_tables(conn)
    if status not in _V1_STATUSES:
        raise ValueError("status must be one of: " + ", ".join(_V1_STATUSES))
    now = utc_iso()
    cur = conn.execute(
        "UPDATE incidents SET status = ?, ended_at = CASE WHEN ? IN ('resolved','closed') "
        "THEN COALESCE(ended_at, ?) ELSE ended_at END, updated_at = ? "
        "WHERE incident_id = ?", (status, status, now, now, incident_id))
    if cur.rowcount == 0:
        raise KeyError(incident_id)
    conn.execute(
        "INSERT INTO incident_notes (incident_id, at, event, kind) VALUES (?,?,?,'note')",
        (incident_id, now, "Status changed to " + status))
    conn.commit()
    inc = get_incident(conn, incident_id)
    assert inc is not None
    return inc


# ---------------------------------------------------------------------------
# Source failover log (§§35-36) — one row per actual failover event.
# ---------------------------------------------------------------------------

def record_failover(conn: sqlite3.Connection, category: str, from_source: str,
                    to_source: str, reason: str = "") -> int:
    ensure_tables(conn)
    cur = conn.execute(
        """INSERT INTO source_failover
           (category, from_source, to_source, reason, occurred_at)
           VALUES (?,?,?,?,?)""",
        (category, from_source, to_source, reason, utc_iso()))
    conn.commit()
    return int(cur.lastrowid)


def recent_failovers(conn: sqlite3.Connection, limit: int = 10) -> List[Dict[str, Any]]:
    ensure_tables(conn)
    rows = conn.execute(
        "SELECT category, from_source, to_source, reason, occurred_at "
        "FROM source_failover ORDER BY occurred_at DESC LIMIT ?",
        (limit,)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Alert delivery audit (§79) — records ONLY what actually happened.
# ---------------------------------------------------------------------------

def record_delivery(conn: sqlite3.Connection, notification_id: int, channel: str,
                    status: str, provider_message_id: Optional[str] = None,
                    detail: Optional[str] = None) -> int:
    """Log one delivery attempt/result. notification_id references notification_history."""
    ensure_tables(conn)
    cur = conn.execute(
        """INSERT INTO notification_deliveries
           (notification_id, channel, provider_message_id, delivered_at, status, detail)
           VALUES (?,?,?,?,?,?)""",
        (notification_id, channel, provider_message_id, utc_iso(), status, detail))
    conn.commit()
    return int(cur.lastrowid)


def list_deliveries(conn: sqlite3.Connection, alert_id: Optional[str] = None,
                    limit: int = 100) -> List[Dict[str, Any]]:
    """Delivery audit rows, newest first, optionally filtered by alert_id."""
    ensure_tables(conn)
    q = ("""SELECT d.id, nh.alert_id, d.channel, d.status,
                   d.delivered_at AS attempted_at, d.provider_message_id, d.detail
            FROM notification_deliveries d
            LEFT JOIN notification_history nh ON nh.id = d.notification_id""")
    args: List[Any] = []
    if alert_id:
        q += " WHERE nh.alert_id = ?"
        args.append(alert_id)
    q += " ORDER BY d.id DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in conn.execute(q, args).fetchall()]


def delivery_stats(conn: sqlite3.Connection, days: int = 7) -> Dict[str, Any]:
    ensure_tables(conn)
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM notification_deliveries "
        "WHERE substr(delivered_at, 1, 10) >= date('now', ?) GROUP BY status",
        ("-%d days" % days,)).fetchall()
    by_status = {r["status"]: r["n"] for r in rows}
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM notification_deliveries "
        "WHERE substr(delivered_at, 1, 10) >= date('now', ?)",
        ("-%d days" % days,)).fetchone()["n"]
    recent = conn.execute(
        """SELECT d.id, nh.alert_id, d.channel, d.status, d.delivered_at AS attempted_at,
                  d.provider_message_id, d.detail
           FROM notification_deliveries d
           LEFT JOIN notification_history nh ON nh.id = d.notification_id
           ORDER BY d.id DESC LIMIT 25""").fetchall()
    return {
        "days": days,
        "total": total,
        "by_status": by_status,
        "recent": [dict(r) for r in recent],
        "note": "Audit log only — rows are written when a delivery is actually attempted.",
    }
