#!/usr/bin/env python3
"""IBTrACS historical cyclone best-tracks backfill (manual, not hourly).

    python scripts/backfill_ibtracs.py [--basins NI,WP | --all]

Downloads the IBTrACS v04r01 per-basin CSVs (NOAA NCEI, free, no key),
streaming-parses them, and stores track points in cyclone_tracks
(dedup by sid+iso_time). The ALL file is 333 MB, so this uses per-basin
files instead (NI ~ small, WP the largest). Live tropical cyclones are
already covered by the GDACS TC feed (hourly stage); IBTrACS is history.

Basins: NA North Atlantic, EP Eastern Pacific, WP Western Pacific,
NI North Indian, SI South Indian, SP South Pacific, SA South Atlantic.
"""
import argparse
import csv
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from backend.common.http import make_session  # noqa: E402
from backend.common.logging_setup import setup_logging  # noqa: E402
from backend.common.timeutil import utc_iso  # noqa: E402
from backend.db import db as dbmod  # noqa: E402

log = setup_logging("aether.backfill_ibtracs")

BASE_URL = ("https://www.ncei.noaa.gov/data/international-best-track-"
            "archive-for-climate-stewardship-ibtracs/v04r01/access/csv/"
            "ibtracs.{basin}.list.v04r01.csv")
ALL_BASINS = ["NA", "EP", "WP", "NI", "SI", "SP", "SA"]
DEFAULT_BASINS = ["NI", "WP"]  # Asia-relevant: Bay of Bengal + W Pacific

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS cyclone_tracks (
    sid TEXT NOT NULL,
    name TEXT,
    basin TEXT,
    season INTEGER,
    iso_time TEXT NOT NULL,
    latitude REAL,
    longitude REAL,
    wind_kt REAL,
    pressure_hpa REAL,
    nature TEXT,
    collected_at TEXT NOT NULL,
    PRIMARY KEY (sid, iso_time)
)
"""


def _f(row, key):
    try:
        v = row.get(key)
        return float(v) if v not in (None, "", " ") else None
    except (TypeError, ValueError):
        return None


def _download(url: str, session, tmp: Path, attempts: int = 4) -> dict:
    """Download `url` to `tmp`, resuming with HTTP Range after breaks.

    The egress proxy breaks long downloads (IncompleteRead at 80-98%);
    a partial file is kept and resumed on the next attempt. Returns
    {"ok": True} or {"ok": False, "error": ...}.
    """
    for attempt in range(1, attempts + 1):
        have = tmp.stat().st_size if tmp.exists() else 0
        headers = {"Range": "bytes=%d-" % have} if have else {}
        try:
            with session.get(url, stream=True, timeout=600,
                             headers=headers) as resp:
                if resp.status_code not in (200, 206):
                    return {"ok": False,
                            "error": "IBTrACS HTTP %s" % resp.status_code}
                if resp.status_code == 200 and have:
                    have = 0  # server ignored Range: restart from zero
                total = None
                content_range = resp.headers.get("Content-Range")
                if content_range and "/" in content_range:
                    try:
                        total = int(content_range.rsplit("/", 1)[1])
                    except ValueError:
                        total = None
                elif resp.headers.get("Content-Length"):
                    try:
                        total = int(resp.headers["Content-Length"]) + have
                    except ValueError:
                        total = None
                mode = "ab" if have else "wb"
                with open(tmp, mode) as fh:
                    for chunk in resp.iter_content(chunk_size=1 << 20):
                        if chunk:
                            fh.write(chunk)
                done = tmp.stat().st_size
                if total is None or done >= total:
                    return {"ok": True}
                log.warning("ibtracs: %s incomplete (%d/%d bytes, "
                            "attempt %d/%d); resuming",
                            tmp.name, done, total, attempt, attempts)
        except Exception as exc:  # noqa: BLE001 - transport failure
            log.warning("ibtracs download attempt %d/%d failed: %s",
                        attempt, attempts, str(exc)[:200])
    return {"ok": False,
            "error": "download incomplete after %d attempts" % attempts}


def backfill_basin(conn, session, basin: str) -> dict:
    url = BASE_URL.format(basin=basin)
    tmp = REPO_ROOT / "data" / (".ibtracs_%s.csv" % basin)
    dl = _download(url, session, tmp)
    if not dl["ok"]:
        return {"basin": basin, "status": "source_unavailable",
                "error": dl["error"]}
    conn.execute(CREATE_SQL)
    now = utc_iso()
    parsed = inserted = 0
    batch = []
    try:
        with open(tmp, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                parsed += 1
                sid = (row.get("SID") or "").strip()
                iso_time = (row.get("ISO_TIME") or "").strip()
                if not sid or not iso_time:
                    continue
                lat = _f(row, "LAT")
                lon = _f(row, "LON")
                if lat is None or lon is None:
                    continue
                if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
                    continue
                season = None
                try:
                    season = int(row.get("SEASON") or 0) or None
                except ValueError:
                    season = None
                batch.append((sid, (row.get("NAME") or "").strip(), basin,
                              season, iso_time, lat, lon,
                              _f(row, "USA_WIND"), _f(row, "USA_PRES"),
                              (row.get("NATURE") or "").strip(), now))
                if len(batch) >= 10000:
                    cur = conn.executemany(
                        "INSERT OR IGNORE INTO cyclone_tracks "
                        "(sid, name, basin, season, iso_time, latitude,"
                        " longitude, wind_kt, pressure_hpa, nature,"
                        " collected_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", batch)
                    inserted += cur.rowcount
                    conn.commit()
                    batch = []
        if batch:
            cur = conn.executemany(
                "INSERT OR IGNORE INTO cyclone_tracks "
                "(sid, name, basin, season, iso_time, latitude, longitude,"
                " wind_kt, pressure_hpa, nature, collected_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)", batch)
            inserted += cur.rowcount
            conn.commit()
    finally:
        tmp.unlink(missing_ok=True)
    return {"basin": basin, "status": "success", "parsed": parsed,
            "inserted": inserted, "skipped_duplicate": parsed - inserted}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--basins", default=",".join(DEFAULT_BASINS))
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()
    basins = ALL_BASINS if args.all else [b.strip().upper()
                                          for b in args.basins.split(",") if b.strip()]
    conn = dbmod.connect()
    session = make_session()
    try:
        results = [backfill_basin(conn, session, b) for b in basins]
    finally:
        conn.close()
    print(json.dumps({"results": results}, indent=1))
    return 0 if all(r["status"] == "success" for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
