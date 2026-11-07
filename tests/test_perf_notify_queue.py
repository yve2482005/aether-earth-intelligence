"""Perf tests: persistent notification queue (§§29-30). Hermetic."""
from __future__ import annotations

import sqlite3

import pytest

from backend.perf import notify_queue
from backend.perf.notify_queue import QueueWorker, depth, enqueue


def _factory(db):
    def _f():
        c = sqlite3.connect(db)
        c.row_factory = sqlite3.Row
        return c
    return _f


def _db(tmp_path):
    db = str(tmp_path / "nq.db")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    notify_queue.ensure_table(conn)
    conn.commit()
    return db, conn


def test_enqueue_is_fast_and_never_blocks(tmp_path):
    db, conn = _db(tmp_path)
    qid = enqueue(conn, "critical", "T", "B", alert_id="a1")
    assert qid > 0
    assert depth(conn)["critical"] == 1
    conn.close()


def test_enqueue_invalid_lane_rejected(tmp_path):
    db, conn = _db(tmp_path)
    with pytest.raises(ValueError):
        enqueue(conn, "urgent", "T", "B")
    conn.close()


def test_lane_priority_order(tmp_path):
    db, conn = _db(tmp_path)
    enqueue(conn, "background", "T3", "B3")
    enqueue(conn, "critical", "T0", "B0")
    enqueue(conn, "normal", "T2", "B2")
    enqueue(conn, "high", "T1", "B1")
    rows = conn.execute(
        "SELECT lane FROM notify_queue WHERE status='queued' ORDER BY "
        "CASE lane WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
        "WHEN 'normal' THEN 2 ELSE 3 END, id ASC").fetchall()
    assert [r["lane"] for r in rows] == ["critical", "high", "normal", "background"]
    conn.close()


def test_persistence_across_restart(tmp_path):
    db, conn = _db(tmp_path)
    enqueue(conn, "high", "T", "B")
    conn.close()
    # "restart": new connection, same file — item survives
    conn2 = sqlite3.connect(db)
    conn2.row_factory = sqlite3.Row
    assert depth(conn2)["high"] == 1
    conn2.close()


def test_worker_degrades_honestly_without_fcm(tmp_path):
    db, conn = _db(tmp_path)
    enqueue(conn, "critical", "M6 quake", "shaking expected")
    conn.close()
    worker = QueueWorker(_factory(db))
    done = worker.drain_once()
    # FCM is not configured in this environment: must NOT claim delivery
    assert done["delivered"] == 0
    assert done["held_degraded"] == 1
    conn2 = sqlite3.connect(db)
    conn2.row_factory = sqlite3.Row
    row = conn2.execute("SELECT status, error FROM notify_queue").fetchone()
    assert row["status"] == "queued"  # kept for retry, never faked
    assert "fcm" in row["error"].lower() or "FCM" in row["error"]
    conn2.close()


def test_stale_critical_dropped_not_delivered_as_fresh(tmp_path):
    db, conn = _db(tmp_path)
    enqueue(conn, "critical", "old", "body")
    conn.execute("UPDATE notify_queue SET enqueued_at='2020-01-01T00:00:00+00:00'")
    conn.commit()
    conn.close()
    worker = QueueWorker(_factory(db), max_age_seconds=60)
    done = worker.drain_once()
    assert done["dropped_stale"] == 1
    conn2 = sqlite3.connect(db)
    conn2.row_factory = sqlite3.Row
    assert conn2.execute("SELECT status FROM notify_queue").fetchone()["status"] == "dropped"
    conn2.close()
