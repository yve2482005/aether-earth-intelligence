#!/usr/bin/env python3
"""Advanced collector runner — drop-in equivalent of run_collect.py.

Same collectors, same locations, same tiers and hour-guard, plus:
parallel location fan-out, an ingest validation gate (out-of-range
weather rows rejected + counted, never stored), gap detection with
backfill over the past 48 h, and a run ledger in ingestion_runs.

Prints the run summary as JSON. Exit code is 0 only when no collector
hard-failed (per-source failures isolated by the collectors are listed
in "source_failures" and do not change the exit code, mirroring
run_collect's isolation semantics). Idempotent: forecast inserts are
INSERT OR IGNORE under the hour-truncated forecast_run, exactly as in
run_collect, so re-running inside the same hour adds no rows.

Usage: run_collect_advanced.py [--max-workers N] [--no-backfill]
"""
import argparse
import faulthandler
import json
import signal
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from backend.collectors.advanced import run_advanced_collection  # noqa: E402

# Diagnostics (added 2026-10-18): SIGUSR1 dumps every thread's stack to
# data/stack_dump.log — used to catch which collector code path holds
# the SQLite write lock during the recurring lock storms (ptrace-based
# stack tools are blocked in this sandbox). Harmless in normal runs.
_STACK_FH = open(REPO_ROOT / "data" / "stack_dump.log", "a", buffering=1)
faulthandler.register(signal.SIGUSR1, file=_STACK_FH, all_threads=True)

# SIGUSR2 dumps the _TimedConnection registry (which connection holds
# an open transaction, which thread created it, its last write) to
# data/txn_state.log — the kernel showed the storm's write-lock holder
# is an idle open transaction inside this process; stacks cannot see
# idle connections, this can.
_TXN_FH = open(REPO_ROOT / "data" / "txn_state.log", "a", buffering=1)


def _dump_txn_state(signum, frame):  # noqa: ANN001, ARG001
    try:
        from backend.db.db import _TimedConnection

        _TimedConnection.dump_txn_state(_TXN_FH)
    except Exception:  # noqa: BLE001 - diagnostics must never crash
        pass


signal.signal(signal.SIGUSR2, _dump_txn_state)


def main() -> int:
    ap = argparse.ArgumentParser(description="AETHER advanced collector")
    ap.add_argument("--max-workers", type=int, default=2,
                    help="parallel location workers (default 2; cut from 4 on 2026-10-18 — the 4-way write queue + 5x192 MB caches were feeding the lock storms)")
    ap.add_argument("--no-backfill", action="store_true",
                    help="detect gaps but do not re-fetch to fill them")
    ap.add_argument("--max-locations-per-run", type=int, default=80,
                    help="fan-out window size; locations rotate across runs (default 80)")
    args = ap.parse_args()
    # Single-instance guard (added 2026-10-17 after the ~1,000-location
    # scale-up): a full sweep can outlast the 5-minute cadence, and
    # overlapping collectors deadlocked each other on the SQLite writer
    # lock (57 "database is locked" errors, runs stuck at 'running',
    # earthquake/tsunami stage starved). If another instance holds the
    # lock, exit 0 immediately — collection is already in progress and
    # the run is idempotent, so skipping loses nothing.
    import fcntl
    lock_path = REPO_ROOT / "data" / ".collect_advanced.lock"
    lock_file = open(lock_path, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(json.dumps({"status": "skipped_single_instance",
                          "reason": "another advanced collector is running"}))
        return 0
    summary = run_advanced_collection(
        max_workers=args.max_workers,
        backfill=not args.no_backfill,
        max_locations_per_run=args.max_locations_per_run)
    print(json.dumps(summary, indent=1, default=str))
    return 0 if not summary["hard_failures"] else 1


if __name__ == "__main__":
    sys.exit(main())
