#!/usr/bin/env python3
"""Data retention enforcement for AETHER (§69 cost governor).

Deletes rows older than the documented retention windows (see
docs/OPERATIONS.md "Data retention policy"). DRY RUN BY DEFAULT — pass
--apply to actually delete. The scientific record (earthquakes, tsunamis,
alerts, model registry) is NEVER auto-pruned.

Policy summary:
    weather_forecasts      NEVER pruned (ML training data — owner rule 2026-10-16)
    weather_observations   NEVER pruned (ML training data — owner rule 2026-10-16)
    system_health          checked_at   older than 180 days -> prune
    data_quality           checked_at   older than 180 days -> prune
    ingestion_runs         started_at   older than 180 days -> prune
    notification_history   created_at   older than 730 days -> prune
    notification_deliveries (via notification_history ids)  -> prune
    alert_revisions        changed_at   older than 730 days -> prune
    everything else: no automatic pruning.

Usage:
    python scripts/prune.py                 # dry run, prints counts
    python scripts/prune.py --apply         # actually delete
    python scripts/prune.py --apply --table weather_forecasts
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from backend.common.config import DB_PATH  # noqa: E402
from backend.common.logging_setup import setup_logging  # noqa: E402
from backend.db import db as dbmod  # noqa: E402

log = setup_logging("aether.prune")

# (table, time_column, retention_days). Never add scientific-record tables here.
# weather_forecasts / weather_observations are ML training data and are NEVER
# auto-pruned (owner rule 2026-10-16: "Training data မဖျက်နဲ့"). A 45-day
# forecast window was briefly adopted and reversed the same day before any
# row was ever deleted (the single --apply run removed 0 rows).
RETENTION = [
    ("system_health", "checked_at", 180),
    ("data_quality", "checked_at", 180),
    ("ingestion_runs", "started_at", 180),
    ("notification_history", "created_at", 730),
    ("alert_revisions", "changed_at", 730),
]


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def plan(conn: sqlite3.Connection, only: str | None = None) -> list[tuple]:
    """Return [(table, cutoff, count)] for rows that would be pruned."""
    rows = []
    for table, col, days in RETENTION:
        if only and table != only:
            continue
        cutoff = _iso(datetime.now(timezone.utc) - timedelta(days=days))
        cur = conn.execute(
            f"SELECT COUNT(*) AS c FROM {table} WHERE {col} < ?", (cutoff,))
        rows.append((table, cutoff, int(cur.fetchone()["c"])))
    return rows


def prune(apply: bool = False, only: str | None = None,
          path=None) -> dict:
    conn = dbmod.connect(path)
    try:
        report = plan(conn, only)
        deleted = {}
        if apply:
            for table, cutoff, count in report:
                col = next(c for t, c, _ in RETENTION if t == table)
                if table == "notification_history":
                    # cascade: drop orphan delivery receipts first
                    conn.execute(
                        """DELETE FROM notification_deliveries
                           WHERE notification_id IN
                           (SELECT id FROM notification_history WHERE created_at < ?)""",
                        (cutoff,))
                conn.execute(f"DELETE FROM {table} WHERE {col} < ?", (cutoff,))
                deleted[table] = count
            conn.commit()
        return {"dry_run": not apply, "plan": report, "deleted": deleted}
    finally:
        conn.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="AETHER data retention (dry run by default)")
    ap.add_argument("--apply", action="store_true",
                    help="actually delete (default: dry run, print only)")
    ap.add_argument("--table", default=None, choices=[t for t, _, _ in RETENTION],
                    help="prune only this table")
    args = ap.parse_args(argv)

    result = prune(apply=args.apply, only=args.table)
    mode = "DRY RUN" if result["dry_run"] else "APPLIED"
    print(f"retention {mode} (db: {DB_PATH})")
    total = 0
    for table, cutoff, count in result["plan"]:
        print(f"  {table}: {count} rows older than {cutoff}")
        total += count
    print(f"total rows {'that would be pruned' if result['dry_run'] else 'pruned'}: {total}")
    log.info("prune %s: %d rows across %d tables",
             mode.lower(), total, len(result["plan"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
