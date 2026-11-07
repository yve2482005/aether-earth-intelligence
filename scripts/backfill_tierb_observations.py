"""Backfill Tier B observations from the Open-Meteo archive API (ERA5).

Tier B locations collected zero nowcast observations (fan-out bug fixed
2026-11-06) and their forecasts went stale on 2026-10-18. This script
gives every Tier B location an ERA5 observation history (source
'era5-archive', quality 'reanalysis') so per-location accuracy is
computable now; forward nowcasts remain the live source.

Safe to re-run: INSERT OR IGNORE on UNIQUE(location_id, source,
observed_at) skips already-stored hours.

Usage:
    python scripts/backfill_tierb_observations.py [--start YYYY-MM-DD]
        [--end YYYY-MM-DD] [--limit N] [--location-ids 56,57,58]
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.collectors.weather import archive_backfill  # noqa: E402
from backend.common.http import make_session  # noqa: E402
from backend.db import db as dbmod  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-10-20")
    ap.add_argument("--end", default="2026-11-06")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--location-ids", default="")
    args = ap.parse_args()

    dbmod.init_db()
    conn = dbmod.connect()
    if args.location_ids:
        ids = [int(x) for x in args.location_ids.split(",") if x.strip()]
        locs = conn.execute(
            "SELECT id, name, latitude, longitude FROM locations "
            "WHERE id IN (%s)" % ",".join("?" * len(ids)), ids).fetchall()
    else:
        locs = conn.execute(
            "SELECT id, name, latitude, longitude FROM locations "
            "WHERE collect_every_hours = 6 ORDER BY id").fetchall()
    if args.limit:
        locs = locs[:args.limit]
    print(f"Tier B locations to backfill: {len(locs)} "
          f"({args.start} .. {args.end})")
    session = make_session()
    tot_ins = tot_skip = tot_err = tot_fail = 0
    for i, (lid, name, lat, lon) in enumerate(locs, 1):
        res = archive_backfill.backfill_location(
            conn, lid, lat, lon, args.start, args.end, session=session)
        if res.get("ok"):
            tot_ins += res["inserted"]
            tot_skip += res["skipped"]
            tot_err += res["errors"]
        else:
            tot_fail += 1
            print(f"  [{i}/{len(locs)}] {name} FAILED: {res.get('error')}")
        if i % 50 == 0 or i == len(locs):
            print(f"  [{i}/{len(locs)}] inserted={tot_ins} "
                  f"skipped={tot_skip} errors={tot_err} failed={tot_fail}")
    print(f"DONE inserted={tot_ins} skipped={tot_skip} errors={tot_err} "
          f"failed_locations={tot_fail}")
    conn.close()
    return 0 if tot_fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
