"""Perf tests: schedule (§4) + priority queues (§5). Hermetic."""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta, timezone

from backend.perf import priority, schedule
from backend.perf.priority import Lane, PriorityQueues, Task, WorkerPool, lane_for_severity


def _mem():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    return c


def test_intervals_have_justifications():
    for src, cfg in schedule.SOURCE_INTERVALS.items():
        assert cfg["interval_seconds"] >= 60, src  # nothing sub-minute
        assert len(str(cfg["justification"])) > 20, src


def test_open_meteo_never_below_15min():
    assert schedule.SOURCE_INTERVALS["open-meteo"]["interval_seconds"] >= 900


def test_usgs_in_1_to_5_min_band():
    iv = schedule.SOURCE_INTERVALS["usgs_quakes"]["interval_seconds"]
    assert 60 <= iv <= 300


def test_due_sources_db():
    conn = _mem()
    schedule.sync_defaults(conn)
    due = schedule.due_sources_db(conn)
    assert set(due) == set(schedule.SOURCE_INTERVALS)  # nothing run yet: all due
    schedule.mark_started(conn, "usgs_quakes")
    schedule.mark_finished(conn, "usgs_quakes", True)
    due2 = schedule.due_sources_db(conn)
    assert "usgs_quakes" not in due2
    # simulate interval elapsed
    past = (datetime.now(timezone.utc) - timedelta(seconds=1000)).isoformat()
    conn.execute("UPDATE perf_source_schedule SET last_started_at=? WHERE source='usgs_quakes'",
                 (past,))
    conn.commit()
    assert "usgs_quakes" in schedule.due_sources_db(conn)


def test_lane_mapping():
    assert lane_for_severity("critical") == Lane.P0
    assert lane_for_severity("warning") == Lane.P0
    assert lane_for_severity("watch") == Lane.P1
    assert lane_for_severity("advisory") == Lane.P2
    assert lane_for_severity(None) == Lane.P3


def test_p0_served_first():
    q = PriorityQueues()
    q.enqueue(Task(Lane.P3, "bg"))
    q.enqueue(Task(Lane.P0, "life"))
    q.enqueue(Task(Lane.P1, "haz"))
    assert q.dequeue().lane == Lane.P0
    assert q.dequeue().lane == Lane.P1
    assert q.dequeue().lane == Lane.P3


def test_starvation_guard_serves_p3():
    q = PriorityQueues(starvation_every=5)
    q.enqueue(Task(Lane.P3, "bg"))
    served = []
    for _ in range(6):
        q.enqueue(Task(Lane.P1, "haz"))
        served.append(q.dequeue().lane)
    assert Lane.P3 in served  # guard fired within 6 serves


def test_worker_pool_drains():
    q = PriorityQueues()
    done = []
    pool = WorkerPool(q, workers=1, handler=lambda t: done.append(t.kind))
    pool.start()
    q.enqueue(Task(Lane.P2, "k1"))
    q.enqueue(Task(Lane.P0, "k0"))
    deadline = time.time() + 5
    while len(done) < 2 and time.time() < deadline:
        time.sleep(0.05)
    pool.stop()
    assert done == ["k0", "k1"]


def test_queue_depths():
    q = PriorityQueues()
    q.enqueue(Task(Lane.P0, "a"))
    q.enqueue(Task(Lane.P0, "b"))
    d = q.depth()
    assert d["P0 life-safety"] == 2 and len(q) == 2
