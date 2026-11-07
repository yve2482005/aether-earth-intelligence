"""Acceptance verdict recorder (perf spec §§84-122).

Each acceptance test calls verdict() with its category number, verdict
(PASS/FAIL/BLOCKED), and measured evidence. Results accumulate in a JSONL
file under tests/; tests/test_acceptance_report.py (runs last) aggregates
them into docs/ACCEPTANCE_REPORT.md.

§120: FAIL entries carry metric/actual/target/difference/probable_cause/
recommended_fix. BLOCKED entries carry external_dependency/why_it_blocks/
workaround. Nothing is fabricated: only recorded measurements appear.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

RESULTS_PATH = Path(__file__).resolve().parent / ".acceptance_results.jsonl"

CATEGORIES = {
    84.1: "build / migrations / startup",
    85: "ingestion per-source metrics",
    86: "incremental ingestion, duplicate_event_rate=0%",
    87: "parallel vs sequential benchmark",
    88: "cache hit rate",
    89: "100-request single-flight coalescing",
    90: "API p50/p95/p99",
    91: "home bundle timing",
    92: "DB query latency + EXPLAIN plans",
    92.5: "PostGIS plan comparison (SQLite deployment)",
    93: "map viewport/clustering caps",
    94: "ML batch vs single inference",
    95: "precomputation reuse + invalidation",
    96: "synthetic quake e2e + M5.8->M6.1 revision versioning",
    97: "synthetic tsunami fixtures, no invented arrival times",
    98: "alert stage latency, P95 source-to-alert <10s",
    99: "dedup duplicate_notification_rate=0%",
    100: "P0 ahead of P3 queue ordering",
    101: "failure recovery",
    102: "circuit breaker",
    103: "freshness exposure (§66)",
    104: "offline behavior",
    105: "validation never skipped for speed",
    106: "observability metrics + trace IDs",
    107: "resource bounds",
    108: "load: 5xx < 1%",
    109: "concurrency",
    110: "regression vs baseline (>20% degradation = FAIL)",
    111: "accuracy separated from speed",
    112: "accuracy metrics not conflated",
    113: "alert precision/recall (synthetic)",
    114: "security: no secrets anywhere",
    115: "provenance on everything",
    116: "UI performance",
    117: "accessibility",
    118: "full synthetic end-to-end source->notification",
}


def verdict(category: float, result: str, evidence: Optional[Dict[str, Any]] = None,
            fail: Optional[Dict[str, Any]] = None,
            blocked: Optional[Dict[str, Any]] = None) -> None:
    assert result in ("PASS", "FAIL", "BLOCKED"), result
    row = {
        "category": category,
        "name": CATEGORIES.get(category, "?"),
        "verdict": result,
        "evidence": evidence or {},
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    if result == "FAIL":
        row["fail"] = fail or {}
    if result == "BLOCKED":
        row["blocked"] = blocked or {}
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(RESULTS_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def reset_results() -> None:
    if RESULTS_PATH.exists():
        os.remove(RESULTS_PATH)


def load_results() -> Dict[float, Dict[str, Any]]:
    out: Dict[float, Dict[str, Any]] = {}
    if RESULTS_PATH.exists():
        for line in RESULTS_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                r = json.loads(line)
                out[r["category"]] = r  # last write wins
    return out
