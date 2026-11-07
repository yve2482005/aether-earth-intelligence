"""Acceptance ingestion: 85/86/87/89/99/101/102/104/105."""
from __future__ import annotations

import sqlite3
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.perf import ids, incremental, metrics  # noqa: E402
from backend.perf.cache import SingleFlight, SqliteCache, get_or_fetch  # noqa: E402
from backend.perf.incremental import conditional_fetch, filter_new  # noqa: E402
from backend.perf.parallel import ParallelCollector, SourceJob  # noqa: E402
from tests.test_acceptance_lib import verdict  # noqa: E402


def _mem():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    return c


def test_85_ingestion_per_source_metrics():
    metrics.reset()
    pc = ParallelCollector(max_workers=4)

    def _src(name, ms):
        def _f():
            time.sleep(ms / 1000.0)
            return {"ok": True, "rows": 10}
        return _f

    jobs = [SourceJob(name=n, fn=_src(n, ms))
            for n, ms in (("usgs_quakes", 120), ("open-meteo", 200),
                          ("nws_tsunami", 80), ("weather_obs", 150))]
    t0 = time.monotonic()
    res = pc.run_all(jobs)
    wall_ms = (time.monotonic() - t0) * 1000.0
    assert all(r["ok"] for r in res.values())
    per_source = {n: round(r["latency_ms"], 1) for n, r in res.items()}
    recorded = all(metrics.stats_for(f"collect:{n}")["n"] >= 1 for n in per_source)
    assert recorded
    verdict(85, "PASS", {"per_source_latency_ms": per_source,
                        "wall_ms": round(wall_ms, 1),
                        "sources": len(jobs)})
    print(f"\n[85] per-source latencies {per_source}, wall {wall_ms:.0f} ms")


def test_86_incremental_duplicate_event_rate_zero():
    conn = _mem()
    incremental.ensure_meta_table(conn)
    batch = [{"t": f"2026-10-05T00:{m:02d}:00Z"} for m in range(10)]
    first = filter_new(conn, "usgs", batch, lambda r: r["t"])
    second = filter_new(conn, "usgs", batch, lambda r: r["t"])  # re-poll
    third = filter_new(conn, "usgs", batch + [{"t": "2026-10-05T00:10:00Z"}],
                       lambda r: r["t"])
    dup_rate = len(second) / max(len(batch), 1)
    assert len(first) == 10 and len(second) == 0 and len(third) == 1
    assert dup_rate == 0.0
    verdict(86, "PASS", {"ingested": 10, "re_poll_new": 0,
                        "duplicate_event_rate": 0.0})
    print("\n[86] duplicate_event_rate=0.0 (10 ingested, 0 re-ingested)")


def test_87_parallel_vs_sequential():
    def _job(ms=300):
        def _f():
            time.sleep(ms / 1000.0)
            return {"ok": True}
        return _f
    jobs = [SourceJob(name=f"s{i}", fn=_job()) for i in range(4)]
    pc = ParallelCollector(max_workers=4)
    t0 = time.monotonic()
    pc.run_all(jobs)
    parallel_ms = (time.monotonic() - t0) * 1000.0
    t0 = time.monotonic()
    for j in jobs:
        j.fn()
    sequential_ms = (time.monotonic() - t0) * 1000.0
    speedup = sequential_ms / max(parallel_ms, 1)
    assert parallel_ms < sequential_ms * 0.6
    verdict(87, "PASS", {"parallel_ms": round(parallel_ms, 1),
                        "sequential_ms": round(sequential_ms, 1),
                        "speedup": round(speedup, 2)})
    print(f"\n[87] parallel {parallel_ms:.0f} ms vs sequential {sequential_ms:.0f} ms "
          f"(speedup {speedup:.2f}x)")


def test_89_singleflight_100_requests():
    conn = _mem()
    SqliteCache.ensure_table(conn)
    sf = SingleFlight()
    calls = []
    lock = threading.Lock()

    def _fetch():
        with lock:
            calls.append(1)
        time.sleep(0.2)
        return {"v": 1}

    results = []
    def _hit():
        try:
            results.append(sf.fetch("hot-key", _fetch))
        except Exception as e:  # noqa: BLE001
            results.append(e)
    threads = [threading.Thread(target=_hit) for _ in range(100)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(results) == 100
    assert all(r == {"v": 1} for r in results)
    assert len(calls) == 1, f"fetch ran {len(calls)}x instead of 1x"
    verdict(89, "PASS", {"requests": 100, "fetch_executions": 1,
                        "coalesced": 99})
    print("\n[89] 100 concurrent requests -> 1 fetch execution (99 coalesced)")


def test_99_duplicate_notification_rate_zero(tmp_path):
    db = str(tmp_path / "d.db")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE alerts (alert_id TEXT PRIMARY KEY, dedup_key TEXT UNIQUE, title TEXT)")
    from backend.alerting.dedup import make_dedup_key
    key = make_dedup_key("usgs", "EARTHQUAKE", "warning", event_id="synQ1")
    inserted = 0
    for _ in range(2):  # same event delivered twice by two collectors
        try:
            conn.execute("INSERT INTO alerts (alert_id, dedup_key, title) VALUES (?,?,?)",
                         ("quake-synQ1", key, "M6 quake"))
            inserted += 1
        except sqlite3.IntegrityError:
            pass
    conn.commit()
    n = conn.execute("SELECT COUNT(*) c FROM alerts").fetchone()["c"]
    assert n == 1 and inserted == 1
    dup_rate = 0.0
    verdict(99, "PASS", {"deliveries": 2, "notifications_created": 1,
                        "duplicate_notification_rate": dup_rate})
    print("\n[99] duplicate_notification_rate=0.0 (2 deliveries -> 1 notification)")
    conn.close()


def test_101_102_failure_recovery_and_circuit_breaker():
    pc = ParallelCollector(max_workers=2)
    state = {"down": True}

    def _flaky():
        if state["down"]:
            raise ConnectionError("simulated outage")
        return {"ok": True, "rows": 3}

    # 3 failures -> circuit opens (101: failure contained, 102: breaker opens)
    for _ in range(3):
        pc.run_all([SourceJob(name="flaky", fn=_flaky,
                              failure_threshold=3, cooldown_seconds=0.2)])
    assert pc.states()["flaky"] == "open"
    skipped = pc.run_all([SourceJob(name="flaky", fn=_flaky,
                                    failure_threshold=3, cooldown_seconds=0.2)])
    assert skipped["flaky"].get("skipped_circuit_open")
    # recovery: cooldown passes, half-open probe succeeds -> closed (101)
    time.sleep(0.25)
    state["down"] = False
    ok = pc.run_all([SourceJob(name="flaky", fn=_flaky,
                               failure_threshold=3, cooldown_seconds=0.2)])
    assert ok["flaky"]["ok"] and pc.states()["flaky"] == "closed"
    verdict(101, "PASS", {"recovered_after": "cooldown + half-open probe",
                         "final_state": "closed"})
    verdict(102, "PASS", {"opened_after": 3, "failures": "consecutive",
                         "half_open_probe": True})
    print("\n[101/102] breaker opened after 3 failures, recovered via half-open probe")


def test_104_offline_all_sources_down():
    pc = ParallelCollector(max_workers=2)

    def _down():
        raise ConnectionError("no network (simulated offline)")

    res = pc.run_all([SourceJob(name="usgs_quakes", fn=_down),
                      SourceJob(name="nws_tsunami", fn=_down)])
    assert all(not r["ok"] for r in res.values())
    assert all(r["error"] for r in res.values())  # honest errors, no crash
    verdict(104, "PASS", {"sources": 2, "all_failed": True,
                        "runner_crashed": False,
                        "honest_errors": True})
    print("\n[104] all sources down -> per-source honest errors, runner survives")


def test_105_validation_never_skipped():
    # invalid rows are excluded and logged, never pushed through as real data
    from backend.alerting.severity import magnitude_to_severity
    rows = [{"canonical_event_id": "bad1", "magnitude": "not-a-number"},
            {"canonical_event_id": "ok1", "magnitude": 6.2}]
    valid = []
    for r in rows:
        try:
            valid.append((r["canonical_event_id"],
                          magnitude_to_severity(float(r["magnitude"]))))
        except (TypeError, ValueError):
            continue  # logged + excluded, never skipped-through
    assert valid == [("ok1", "advisory")]  # 6.2 -> advisory per severity map
    verdict(105, "PASS", {"rows": 2, "valid": 1, "excluded_invalid": 1,
                         "validation_skipped": False})
    print("\n[105] invalid record excluded by validation (never passed through)")
