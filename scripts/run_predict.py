#!/usr/bin/env python3
"""One-shot ML prediction runner (batch inference, never trains).

Scope (updated 2026-10-17 evening): ALL locations, using the registered
champion model per task from model_versions. The from-scratch retrain
(user order) produced worldwide champions whose artifacts carry
per-location estimators plus a global fallback, so every collected
location is covered (the batch layer still refuses to borrow another
location's model — unrouted locations raise instead). Before that
retrain the champions were Yangon-only and this runner was Yangon-only.

Only forecasts whose valid_time is now or in the future are predicted:
the point of the predictions table is serving upcoming weather, and a
historical backfill would be hundreds of thousands of rows of no
operational value.
Idempotent: rows already predicted for (task, champion version,
location, valid_time) are skipped by the batch layer's LEFT JOIN dedupe,
so re-running is safe at any cadence.

Prints one JSON summary line per task; exit 0 only if every task ran.
"""
import json
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from backend.common.timeutil import utc_iso  # noqa: E402
from backend.db import db as dbmod  # noqa: E402
from backend.perf import ml_batch  # noqa: E402

TASKS = ("temperature", "rain_prob", "precip_amount", "wind", "pressure",
         "humidity")
LIMIT_PER_TASK = 100000


def main() -> int:
    conn = dbmod.connect()
    conn.row_factory = sqlite3.Row
    now = utc_iso()
    failures = 0
    for task in TASKS:
        try:
            out = ml_batch.precompute_predictions(
                conn, task=task, limit=LIMIT_PER_TASK, min_valid_time=now)
            print(json.dumps(out))
        except Exception as exc:  # one task failing never blocks the rest
            failures += 1
            print(json.dumps({"task": task, "error": str(exc)}))
    conn.close()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
