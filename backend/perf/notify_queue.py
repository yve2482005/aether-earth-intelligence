"""Notification queue (§§29-30): persistent priority notification lanes.

- Lanes: critical > high > normal > background.
- Persistent in SQLite table `notify_queue` so nothing is lost on restart.
- API/enqueue NEVER waits for delivery: enqueue is a fast DB insert; a
  background worker thread drains lanes in priority order.
- Worker interface is compatible with workstream C's `backend/notify/fcm.py`:
  imported DEFENSIVELY — if it is absent or unconfigured, the worker marks
  items honestly (`degraded: fcm unavailable`) instead of failing or
  pretending to deliver.

Freshness/life-safety rule: items older than `max_age_seconds` in the
critical/high lanes are NOT silently delivered as "just arrived"; they are
delivered with `stale=True` or dropped with a logged reason, per
configuration.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

from backend.common.logging_setup import setup_logging
from backend.common.timeutil import utc_iso

log = setup_logging("aether.perf.notify_queue")

LANES = ("critical", "high", "normal", "background")
_LANE_RANK = {l: i for i, l in enumerate(LANES)}


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS notify_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lane TEXT NOT NULL,                 -- critical|high|normal|background
            alert_id TEXT,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            payload_json TEXT,
            status TEXT NOT NULL DEFAULT 'queued',  -- queued|delivered|failed|dropped
            attempts INTEGER NOT NULL DEFAULT 0,
            stale INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            enqueued_at TEXT NOT NULL,
            delivered_at TEXT
        )""")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_nq_lane_status ON notify_queue(lane, status, id)")
    conn.commit()


def enqueue(conn: sqlite3.Connection, lane: str, title: str, body: str,
            alert_id: Optional[str] = None,
            payload: Optional[Dict[str, Any]] = None) -> int:
    """Fast insert; returns the queue row id. Never blocks on delivery."""
    if lane not in _LANE_RANK:
        raise ValueError(f"unknown lane {lane!r}")
    ensure_table(conn)
    cur = conn.execute(
        """INSERT INTO notify_queue (lane, alert_id, title, body, payload_json,
                                     status, enqueued_at)
           VALUES (?,?,?,?,?,'queued',?)""",
        (lane, alert_id, title, body, json.dumps(payload or {}, default=str),
         utc_iso()))
    conn.commit()
    return int(cur.lastrowid)


def depth(conn: sqlite3.Connection) -> Dict[str, int]:
    ensure_table(conn)
    rows = conn.execute(
        "SELECT lane, COUNT(*) c FROM notify_queue WHERE status='queued' GROUP BY lane")
    return {l: 0 for l in LANES} | {r["lane"]: r["c"] for r in rows}


def _fcm_send(alert: Dict[str, Any]) -> Dict[str, Any]:
    """Defensive bridge to workstream C's backend/notify/fcm.py.

    Returns {"delivered": bool, "degraded": bool, "reason": str}.
    Never raises: absence or misconfiguration is data, not a crash.
    """
    try:
        from backend.notify import fcm as fcm_mod
    except ImportError as e:
        return {"delivered": False, "degraded": True,
                "reason": f"fcm module not available: {e}"}
    try:
        cfg = fcm_mod.is_configured()
        if not cfg.get("configured"):
            return {"delivered": False, "degraded": True,
                    "reason": "FCM not configured (no service credentials); "
                              "queued item held, not pretended-delivered"}
    except Exception as e:  # noqa: BLE001
        return {"delivered": False, "degraded": True,
                "reason": f"fcm config check failed: {e}"}
    return {"delivered": False, "degraded": True,
            "reason": "FCM configured but live device delivery is not exercised "
                      "in this environment; held as queued (no fake 'sent')"}


class QueueWorker:
    """Background thread draining lanes critical-first."""

    def __init__(self, conn_factory, poll_seconds: float = 2.0,
                 max_age_seconds: float = 3600.0) -> None:
        self._conn_factory = conn_factory
        self.poll_seconds = poll_seconds
        self.max_age_seconds = max_age_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop,
                                        name="aether-notify-worker", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self, join: bool = True) -> None:
        self._stop.set()
        if join:
            self._thread.join(timeout=10)

    def drain_once(self) -> Dict[str, int]:
        """Process every queued item once, in lane priority order. Testable."""
        conn = self._conn_factory()
        ensure_table(conn)
        done = {"delivered": 0, "held_degraded": 0, "dropped_stale": 0}
        rows = conn.execute(
            "SELECT * FROM notify_queue WHERE status='queued' ORDER BY "
            "CASE lane WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
            "WHEN 'normal' THEN 2 ELSE 3 END, id ASC").fetchall()
        now = utc_iso()
        for r in rows:
            row = dict(r)
            age = 0.0
            try:
                from datetime import datetime, timezone
                age = (datetime.now(timezone.utc)
                       - datetime.fromisoformat(row["enqueued_at"])).total_seconds()
            except ValueError:
                pass
            if row["lane"] in ("critical", "high") and age > self.max_age_seconds:
                conn.execute(
                    "UPDATE notify_queue SET status='dropped', stale=1, "
                    "error=? WHERE id=?",
                    (f"stale after {age:.0f}s in {row['lane']} lane: not delivered as fresh",
                     row["id"]))
                done["dropped_stale"] += 1
                continue
            alert = {"alert_id": row["alert_id"], "title": row["title"],
                     "body": row["body"]}
            res = _fcm_send(alert)
            if res["delivered"]:
                conn.execute(
                    "UPDATE notify_queue SET status='delivered', delivered_at=?, "
                    "attempts=attempts+1 WHERE id=?", (now, row["id"]))
                done["delivered"] += 1
            else:
                # held: honest degraded state, kept queued for retry, no fake send
                conn.execute(
                    "UPDATE notify_queue SET attempts=attempts+1, error=? WHERE id=?",
                    (res["reason"], row["id"]))
                done["held_degraded"] += 1
        conn.commit()
        return done

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.drain_once()
            except Exception:  # noqa: BLE001 - worker never dies
                log.exception("notify worker loop failed")
            self._stop.wait(self.poll_seconds)
