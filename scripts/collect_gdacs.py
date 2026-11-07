#!/usr/bin/env python3
"""GDACS disaster-feed collection (standalone stage).

    python scripts/collect_gdacs.py [--days 7]

Fetches GDACS events (earthquake, cyclone, flood, volcano, drought,
wildfire) for the last N days and stores them in gdacs_events
(dedup by eventtype+eventid). Prints one JSON summary. Exit 0 on
success/no_new_data, 1 on source or persistence failure.
"""
import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from backend.collectors.disaster import gdacs  # noqa: E402
from backend.db import db as dbmod  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args()
    conn = dbmod.connect()
    try:
        result = gdacs.collect(conn, days=args.days)
    finally:
        conn.close()
    print(json.dumps(result))
    return 0 if result["status"] in ("success", "no_new_data") else 1


if __name__ == "__main__":
    raise SystemExit(main())
