#!/usr/bin/env python3
"""Bulk raw current-conditions collector for ALL GeoNames-registered places.

User order 2026-10-17: raw data for 200,000 cities. Uses the Open-Meteo
forecast API's multi-coordinate form (one call per batch of places),
validates every reading with the advanced collector's ingest ranges,
and checkpoints progress in the DB so a killed or rate-limited sweep
resumes instead of restarting. On HTTP 429 the sweep stops gracefully.

Requires the registry first: scripts/load_global_places.py

Examples:
  python scripts/collect_global_places.py                 # resume / run
  python scripts/collect_global_places.py --max-batches 50
  python scripts/collect_global_places.py --fresh         # new sweep
"""
import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from backend.collectors.weather import global_places as gp  # noqa: E402
from backend.common.http import make_session  # noqa: E402
from backend.db import db as dbmod  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--batch-size", type=int, default=gp.DEFAULT_BATCH_SIZE)
    ap.add_argument("--max-batches", type=int, default=None,
                    help="stop after this many batches (default: run cap)")
    ap.add_argument("--fresh", action="store_true",
                    help="start a new sweep instead of resuming")
    ap.add_argument("--sleep", type=float, default=0.0,
                    help="seconds to sleep between calls (rate politeness)")
    ap.add_argument("--db", default=None, help="DB path (default: project config)")
    args = ap.parse_args()

    conn = dbmod.connect(args.db) if args.db else dbmod.connect()
    try:
        conn.execute("PRAGMA busy_timeout=60000")
        summary = gp.run_sweep(conn, make_session(),
                               batch_size=args.batch_size,
                               max_batches=args.max_batches,
                               fresh=args.fresh, sleep_s=args.sleep)
    finally:
        conn.close()
    printable = {k: v for k, v in summary.items() if k != "completed"}
    print(json.dumps(printable, indent=1))
    return 0 if summary["status"] in ("complete", "partial", "deferred") else 1


if __name__ == "__main__":
    raise SystemExit(main())
