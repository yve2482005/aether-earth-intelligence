"""Observability (§§58-61): per-stage latency + p50/p95/p99 accumulators.

- `record(stage, label, latency_ms, ok=True)`: record one measurement.
- `summary()` / `details()`: p50/p95/p99 per stage, served by GET /v1/perf/metrics.
- `record_e2e(source_timestamp_iso, ...)`: end-to-end source-timestamp ->
  notification latency; the source's own timestamp (not ours) is the start,
  so clock skew is the source's honest number, not a fabricated one.
- In-process accumulators are bounded (ring buffer per key); a background
  flush can persist them to `perf_metrics` via migrate_perf for dashboards.
- Trace IDs: `new_trace()` / thread-local current trace for cross-stage
  correlation (§106). REAL measured numbers only — nothing estimated.
"""
from __future__ import annotations

import sqlite3
import statistics
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from backend.common.logging_setup import setup_logging

log = setup_logging("aether.perf.metrics")

STAGES = ("fetch", "parse", "validate", "dedup", "db", "model", "alert",
          "notify", "api", "collect", "e2e")

_MAX_SAMPLES = 2048

_lock = threading.Lock()
_samples: Dict[str, deque] = {}          # "stage:label" -> deque[float]
_failures: Dict[str, int] = {}
_thread_local = threading.local()


def new_trace() -> str:
    tid = uuid.uuid4().hex[:16]
    _thread_local.trace_id = tid
    return tid


def current_trace() -> Optional[str]:
    return getattr(_thread_local, "trace_id", None)


def _key(stage: str, label: str) -> str:
    return f"{stage}:{label or '*'}"


def record(stage: str, label: str, latency_ms: float, ok: bool = True,
           trace_id: Optional[str] = None) -> None:
    """Record one real measurement. Never synthesize."""
    k = _key(stage, label)
    with _lock:
        dq = _samples.get(k)
        if dq is None:
            dq = deque(maxlen=_MAX_SAMPLES)
            _samples[k] = dq
        dq.append(float(latency_ms))
        if not ok:
            _failures[k] = _failures.get(k, 0) + 1


def record_e2e(source_timestamp_iso: str, stage_note: str = "source->notify") -> Dict[str, Any]:
    """End-to-end latency from the SOURCE's event timestamp to now.

    The start is the source's own timestamp (honest: includes source pipeline
    delay, which is real and outside our control — §60)."""
    try:
        src = datetime.fromisoformat(source_timestamp_iso)
        if src.tzinfo is None:
            src = src.replace(tzinfo=timezone.utc)
        latency_ms = (datetime.now(timezone.utc) - src).total_seconds() * 1000.0
    except (ValueError, TypeError) as e:
        return {"ok": False, "error": f"bad source timestamp: {e}"}
    record("e2e", stage_note, latency_ms)
    return {"ok": True, "latency_ms": latency_ms, "trace_id": current_trace()}


def _percentile(sorted_vals: List[float], p: float) -> Optional[float]:
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def stats_for(key: str) -> Dict[str, Any]:
    with _lock:
        dq = list(_samples.get(key, []))
        fails = _failures.get(key, 0)
    if not dq:
        return {"n": 0, "failures": fails}
    s = sorted(dq)
    return {
        "n": len(s),
        "failures": fails,
        "min_ms": round(s[0], 2),
        "p50_ms": round(_percentile(s, 50), 2),
        "p95_ms": round(_percentile(s, 95), 2),
        "p99_ms": round(_percentile(s, 99), 2),
        "max_ms": round(s[-1], 2),
        "mean_ms": round(statistics.fmean(s), 2),
    }


def summary() -> Dict[str, Any]:
    """Compact: per-stage rollups across labels."""
    with _lock:
        keys = list(_samples.keys())
    by_stage: Dict[str, List[float]] = {}
    for k in keys:
        stage = k.split(":", 1)[0]
        with _lock:
            by_stage.setdefault(stage, []).extend(_samples[k])
    out: Dict[str, Any] = {}
    for stage, vals in by_stage.items():
        s = sorted(vals)
        out[stage] = {"n": len(s),
                      "p50_ms": round(_percentile(s, 50), 2) if s else None,
                      "p95_ms": round(_percentile(s, 95), 2) if s else None,
                      "p99_ms": round(_percentile(s, 99), 2) if s else None}
    return {"stages": out, "note": "real measured latencies; external source "
            "latency is outside our control (§60), never fabricated"}


def details() -> Dict[str, Any]:
    with _lock:
        keys = list(_samples.keys())
    return {"keys": {k: stats_for(k) for k in keys}}


def reset() -> None:
    with _lock:
        _samples.clear()
        _failures.clear()


def persist(conn: sqlite3.Connection, stage_filter: Optional[str] = None) -> int:
    """Flush accumulator stats to perf_metrics for dashboards."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS perf_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stage TEXT NOT NULL, label TEXT NOT NULL,
            n INTEGER NOT NULL, failures INTEGER NOT NULL,
            p50_ms REAL, p95_ms REAL, p99_ms REAL, max_ms REAL, mean_ms REAL,
            recorded_at TEXT NOT NULL
        )""")
    from backend.common.timeutil import utc_iso
    now = utc_iso()
    count = 0
    with _lock:
        keys = [k for k in _samples if stage_filter is None or k.startswith(stage_filter + ":")]
    for k in keys:
        st = stats_for(k)
        stage, label = k.split(":", 1)
        conn.execute(
            """INSERT INTO perf_metrics (stage, label, n, failures, p50_ms, p95_ms,
                                        p99_ms, max_ms, mean_ms, recorded_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (stage, label, st["n"], st["failures"], st.get("p50_ms"),
             st.get("p95_ms"), st.get("p99_ms"), st.get("max_ms"),
             st.get("mean_ms"), now))
        count += 1
    conn.commit()
    return count
