"""One-off backfill: fetch USGS events by id (FDSN) and upsert into
earthquake_events through the same normalizer + tracker the collector uses,
so rows are exactly as if the feed had caught them.

Usage:
    python scripts/backfill_events.py us7000tw76 us7000twg9
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.common.http import fetch_json, make_session  # noqa: E402
from backend.collectors.earthquake.event_normalizer import normalize_usgs_feature  # noqa: E402
from backend.collectors.earthquake.event_tracker import upsert  # noqa: E402

FDSN = "https://earthquake.usgs.gov/fdsnws/event/1/query"


def main(event_ids: list[str]) -> int:
    conn = sqlite3.connect(ROOT / "data" / "aether.db", timeout=120)
    conn.row_factory = sqlite3.Row
    session = make_session()
    failed = 0
    for eid in event_ids:
        res = fetch_json(session, FDSN, params={"format": "geojson", "eventid": eid})
        if not res["ok"]:
            print(f"{eid}: FETCH FAILED: {res.get('error')}")
            failed += 1
            continue
        data = res["data"] or {}
        # eventid queries return a single Feature; time-window queries
        # return a FeatureCollection.
        if data.get("type") == "Feature":
            features = [data]
        else:
            features = data.get("features") or []
        if not features:
            print(f"{eid}: NOT FOUND in USGS")
            failed += 1
            continue
        rec = normalize_usgs_feature(features[0])
        out = upsert(conn, rec)
        print(f"{eid}: {out.get('action')} mag={rec.get('magnitude')} "
              f"region={rec.get('region')} origin={rec.get('origin_time')}")
        if out.get("action") == "invalid":
            failed += 1
    conn.close()
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:] or ["us7000tw76", "us7000twg9"]))
