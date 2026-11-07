"""Notification history with read/unread state.

Uses the landed `notification_history` table
(id, alert_id, channel, recipient, title, body, status, error, created_at,
sent_at) and adds a `read_at` column (ALTER guard) for read/unread tracking.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional

from backend.common.timeutil import utc_iso

HISTORY_DDL = """CREATE TABLE IF NOT EXISTS notification_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id TEXT,
    channel TEXT NOT NULL,
    recipient TEXT,
    title TEXT, body TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    error TEXT,
    created_at TEXT NOT NULL, sent_at TEXT
);"""


def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(HISTORY_DDL)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(notification_history)").fetchall()}
    if "read_at" not in cols:
        conn.execute("ALTER TABLE notification_history ADD COLUMN read_at TEXT")
    conn.commit()


def record(conn: sqlite3.Connection, *, alert_id: Optional[str],
           channel: str, recipient: Optional[str] = None,
           title: Optional[str] = None, body: Optional[str] = None,
           status: str = "queued", error: Optional[str] = None,
           sent_at: Optional[str] = None,
           provider_message_id: Optional[str] = None) -> int:
    """Append one history row. Returns the row id."""
    ensure_tables(conn)
    cur = conn.execute(
        """INSERT INTO notification_history
           (alert_id, channel, recipient, title, body, status, error, created_at, sent_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (alert_id, channel, recipient, title, body, status, error,
         utc_iso(), sent_at))
    nid = cur.lastrowid
    if provider_message_id:
        conn.execute("""CREATE TABLE IF NOT EXISTS notification_deliveries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            notification_id INTEGER NOT NULL,
            channel TEXT NOT NULL, provider_message_id TEXT,
            delivered_at TEXT, status TEXT NOT NULL, detail TEXT)""")
        conn.execute(
            """INSERT INTO notification_deliveries
               (notification_id, channel, provider_message_id, delivered_at, status)
               VALUES (?,?,?,?,?)""",
            (nid, channel, provider_message_id, utc_iso(), "delivered"))
    conn.commit()
    return nid


def mark_read(conn: sqlite3.Connection, notification_id: int) -> bool:
    ensure_tables(conn)
    cur = conn.execute(
        "UPDATE notification_history SET read_at = ? WHERE id = ?",
        (utc_iso(), notification_id))
    conn.commit()
    return cur.rowcount > 0


def mark_all_read(conn: sqlite3.Connection,
                  recipient: Optional[str] = None) -> int:
    ensure_tables(conn)
    sql = "UPDATE notification_history SET read_at = ? WHERE read_at IS NULL"
    params: list = [utc_iso()]
    if recipient:
        sql += " AND recipient = ?"
        params.append(recipient)
    cur = conn.execute(sql, params)
    conn.commit()
    return cur.rowcount


def unread_count(conn: sqlite3.Connection,
                 recipient: Optional[str] = None) -> int:
    ensure_tables(conn)
    sql = "SELECT COUNT(*) AS c FROM notification_history WHERE read_at IS NULL"
    params: list = []
    if recipient:
        sql += " AND recipient = ?"
        params.append(recipient)
    return conn.execute(sql, params).fetchone()["c"]


def list_history(conn: sqlite3.Connection,
                 recipient: Optional[str] = None,
                 unread_only: bool = False,
                 limit: int = 100) -> List[Dict[str, Any]]:
    ensure_tables(conn)
    sql = "SELECT * FROM notification_history"
    clauses, params = [], []
    if recipient:
        clauses.append("recipient = ?")
        params.append(recipient)
    if unread_only:
        clauses.append("read_at IS NULL")
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(sql, params).fetchall()]
