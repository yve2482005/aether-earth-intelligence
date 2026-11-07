"""Parallel collectors (§3): ThreadPoolExecutor fan-out over EXISTING collector functions.

We import, never rewrite, the collectors. Each source gets:
  - per-source timeout (sources must not block each other)
  - per-source latency recording into metrics
  - a circuit breaker: opens after N consecutive failures, half-open probe
    after a cooldown lets one probe through.

Isolation rule: one source failing (exception, timeout, HTTP error) never
cancels the others; it is reported honestly in the result dict.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from backend.common.logging_setup import setup_logging
from backend.perf import metrics as metrics_mod

log = setup_logging("aether.perf.parallel")


@dataclass
class CircuitBreaker:
    """Per-source circuit breaker. States: closed -> open -> half-open -> closed."""
    failure_threshold: int = 3
    cooldown_seconds: float = 120.0
    _failures: int = 0
    _state: str = "closed"
    _opened_at: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def state(self) -> str:
        return self._state

    def allow(self) -> bool:
        with self._lock:
            if self._state == "closed":
                return True
            if self._state == "open":
                if time.monotonic() - self._opened_at >= self.cooldown_seconds:
                    self._state = "half-open"
                    return True
                return False
            return True  # half-open: single probe allowed through

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._state = "closed"

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._state == "half-open":
                self._state = "open"
                self._opened_at = time.monotonic()
            elif self._failures >= self.failure_threshold:
                self._state = "open"
                self._opened_at = time.monotonic()


@dataclass
class SourceJob:
    name: str
    fn: Callable[[], Dict[str, Any]]
    timeout_seconds: float = 90.0
    failure_threshold: int = 3
    cooldown_seconds: float = 120.0


class ParallelCollector:
    """Run a set of SourceJobs concurrently with per-source circuit breakers.

    `run_all` returns {name: {"ok": bool, "latency_ms": float, "result"|"error",
    "skipped_circuit_open": bool, ...}} — honest per-source outcomes.
    """

    def __init__(self, max_workers: int = 6) -> None:
        self.max_workers = max_workers
        self._breakers: Dict[str, CircuitBreaker] = {}
        self._lock = threading.Lock()

    def breaker(self, name: str, threshold: int = 3,
                cooldown: float = 120.0) -> CircuitBreaker:
        with self._lock:
            cb = self._breakers.get(name)
            if cb is None:
                cb = CircuitBreaker(failure_threshold=threshold,
                                    cooldown_seconds=cooldown)
                self._breakers[name] = cb
            return cb

    def _run_one(self, job: SourceJob) -> Dict[str, Any]:
        cb = self.breaker(job.name, job.failure_threshold, job.cooldown_seconds)
        if not cb.allow():
            return {"ok": False, "skipped_circuit_open": True,
                    "error": "circuit breaker open (too many recent failures)",
                    "latency_ms": 0.0, "breaker_state": "open"}
        t0 = time.monotonic()
        ex = ThreadPoolExecutor(max_workers=1)
        try:
            future = ex.submit(job.fn)
            result = future.result(timeout=job.timeout_seconds)
        except FuturesTimeout:
            ex.shutdown(wait=False, cancel_futures=True)
            latency_ms = (time.monotonic() - t0) * 1000.0
            cb.record_failure()
            metrics_mod.record("collect", job.name, latency_ms, ok=False)
            return {"ok": False, "error": f"timeout after {job.timeout_seconds}s",
                    "latency_ms": latency_ms, "breaker_state": cb.state}
        except Exception as e:  # noqa: BLE001 - isolation per source
            ex.shutdown(wait=True)
            latency_ms = (time.monotonic() - t0) * 1000.0
            cb.record_failure()
            metrics_mod.record("collect", job.name, latency_ms, ok=False)
            return {"ok": False, "error": f"{type(e).__name__}: {e}",
                    "latency_ms": latency_ms, "breaker_state": cb.state}
        ex.shutdown(wait=True)
        latency_ms = (time.monotonic() - t0) * 1000.0
        cb.record_success()
        metrics_mod.record("collect", job.name, latency_ms)
        return {"ok": True, "latency_ms": latency_ms, "result": result,
                "breaker_state": cb.state}

    def run_all(self, jobs: List[SourceJob]) -> Dict[str, Dict[str, Any]]:
        results: Dict[str, Dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=self.max_workers,
                                thread_name_prefix="aether-collect") as ex:
            futures = {ex.submit(self._run_one, job): job.name for job in jobs}
            for fut, name in futures.items():
                try:
                    results[name] = fut.result()
                except Exception as e:  # noqa: BLE001 - never lose a source
                    results[name] = {"ok": False, "error": f"runner error: {e}",
                                     "latency_ms": 0.0}
        return results

    def states(self) -> Dict[str, str]:
        with self._lock:
            return {n: cb.state for n, cb in self._breakers.items()}
