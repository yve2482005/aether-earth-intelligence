"""Perf tests: metrics accumulators + trace IDs (§§58-61, 106). Hermetic."""
from __future__ import annotations

import sqlite3
import threading

from backend.perf import metrics
from backend.perf.metrics import (current_trace, details, new_trace, persist,
                                  record, record_e2e, reset, stats_for, summary)


def setup_function(_):
    reset()


def test_percentiles():
    for i in range(1, 101):
        record("api", "x", float(i))
    s = stats_for("api:x")
    assert s["n"] == 100
    assert s["p50_ms"] == 50.5
    assert 94 <= s["p95_ms"] <= 96
    assert s["min_ms"] == 1.0 and s["max_ms"] == 100.0


def test_failures_counted():
    record("collect", "usgs", 10.0, ok=False)
    s = stats_for("collect:usgs")
    assert s["failures"] == 1


def test_summary_rollup_per_stage():
    record("api", "a", 10.0)
    record("api", "b", 20.0)
    record("db", "c", 5.0)
    sm = summary()["stages"]
    assert sm["api"]["n"] == 2 and sm["db"]["n"] == 1
    assert sm["api"]["p50_ms"] == 15.0


def test_e2e_uses_source_timestamp():
    from datetime import datetime, timedelta, timezone
    src = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    r = record_e2e(src)
    assert r["ok"] and 4000 < r["latency_ms"] < 60000


def test_e2e_bad_timestamp_is_data_not_crash():
    r = record_e2e("not-a-time")
    assert r["ok"] is False


def test_trace_ids_correlate_stages():
    t = new_trace()
    assert current_trace() == t and len(t) == 16


def test_thread_safety_of_record():
    def _w():
        for i in range(200):
            record("api", "t", float(i))
    ts = [threading.Thread(target=_w) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert stats_for("api:t")["n"] == 800


def test_persist_to_db():
    record("api", "p", 12.0)
    conn = sqlite3.connect(":memory:")
    n = persist(conn, stage_filter="api")
    assert n == 1
    row = conn.execute("SELECT stage, p50_ms FROM perf_metrics").fetchone()
    assert row[0] == "api" and row[1] == 12.0
    conn.close()


def test_bounded_memory():
    for i in range(5000):
        record("api", "big", float(i))
    assert stats_for("api:big")["n"] == 2048  # ring buffer bound
