"""Perf tests: parallel collectors + circuit breakers (§3). Hermetic: no network."""
from __future__ import annotations

import time

from backend.perf import metrics
from backend.perf.parallel import CircuitBreaker, ParallelCollector, SourceJob


def _ok():
    return {"ok": True, "n": 1}


def _fail():
    raise RuntimeError("boom")


def _slow():
    time.sleep(5)
    return {"ok": True}


def test_parallel_runs_sources_concurrently():
    pc = ParallelCollector(max_workers=4)
    jobs = [SourceJob(name=f"s{i}", fn=lambda: {"ok": True, "i": 1}) for i in range(4)]
    t0 = time.monotonic()
    res = pc.run_all(jobs)
    dt = time.monotonic() - t0
    assert len(res) == 4 and all(r["ok"] for r in res.values())
    assert dt < 5  # all four ran in one fan-out, not sequentially
    for r in res.values():
        assert r["latency_ms"] >= 0


def test_one_source_failing_does_not_cancel_others():
    pc = ParallelCollector(max_workers=2)
    res = pc.run_all([SourceJob(name="bad", fn=_fail),
                      SourceJob(name="good", fn=_ok)])
    assert res["bad"]["ok"] is False and "boom" in res["bad"]["error"]
    assert res["good"]["ok"] is True
    assert res["bad"]["breaker_state"] in ("closed", "open", "half-open")


def test_circuit_breaker_opens_after_threshold():
    cb = CircuitBreaker(failure_threshold=3, cooldown_seconds=60)
    for _ in range(3):
        assert cb.allow()
        cb.record_failure()
    assert cb.state == "open"
    assert not cb.allow()  # open: no pass-through
    cb2 = CircuitBreaker(failure_threshold=2, cooldown_seconds=0.01)
    cb2.record_failure(); cb2.record_failure()
    assert cb2.state == "open"
    time.sleep(0.02)
    assert cb2.allow()  # half-open probe
    cb2.record_success()
    assert cb2.state == "closed"


def test_circuit_breaker_integration_skips_open_source():
    pc = ParallelCollector(max_workers=1)
    for _ in range(3):
        pc.run_all([SourceJob(name="flaky", fn=_fail,
                              failure_threshold=3, cooldown_seconds=60)])
    res = pc.run_all([SourceJob(name="flaky", fn=_ok,
                                failure_threshold=3, cooldown_seconds=60)])
    assert res["flaky"]["ok"] is False
    assert res["flaky"].get("skipped_circuit_open") is True


def test_timeout_is_reported_not_hung():
    pc = ParallelCollector(max_workers=1)
    t0 = time.monotonic()
    res = pc.run_all([SourceJob(name="hang", fn=_slow, timeout_seconds=0.5)])
    dt = time.monotonic() - t0
    assert res["hang"]["ok"] is False and "timeout" in res["hang"]["error"]
    assert dt < 4


def test_latency_recorded_into_metrics():
    metrics.reset()
    pc = ParallelCollector(max_workers=1)
    pc.run_all([SourceJob(name="metered", fn=_ok)])
    s = metrics.stats_for("collect:metered")
    assert s["n"] >= 1 and s["p50_ms"] is not None
