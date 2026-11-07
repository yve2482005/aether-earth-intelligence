"""NASA FIRMS wildfire detections — key-gated adapter (Engine 1).

Source: NASA FIRMS (Fire Information for Resource Management System),
VIIRS SNPP near-real-time active-fire detections.
Free, but needs a MAP_KEY from https://firms.modaps.eosdis.nasa.gov/api/area/
(the user registers, puts the key in FIRMS_MAP_KEY via the secure vault).

Status: pending_verification — no key is available in this environment,
so the adapter is DISABLED until a key is set, and no live call has been
made. A hotspot is a satellite detection, never a confirmed wildfire.

Endpoint shape (documented): GET
  https://firms.modaps.eosdis.nasa.gov/api/area/csv/{MAP_KEY}/VIIRS_SNPP_NRT/{west},{south},{east},{north}/{days}
CSV columns: latitude, longitude, bright_ti4, scan, track, acq_date,
acq_time, satellite, instrument, confidence, version, bright_ti5, frp,
daynight.
"""
from __future__ import annotations

import csv
import io
import os
import sqlite3
from typing import Any, Dict, List, Optional

from backend.common.http import make_session
from backend.common.logging_setup import setup_logging
from backend.common.timeutil import utc_iso

log = setup_logging("aether.collector.firms")

FIRMS_URL = ("https://firms.modaps.eosdis.nasa.gov/api/area/csv/"
             "{key}/VIIRS_SNPP_NRT/{bbox}/{days}")
ENV_KEY = "FIRMS_MAP_KEY"

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS firms_detections (
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    acq_date TEXT NOT NULL,
    acq_time TEXT NOT NULL,
    satellite TEXT,
    instrument TEXT,
    confidence TEXT,
    frp REAL,
    daynight TEXT,
    collected_at TEXT NOT NULL,
    PRIMARY KEY (latitude, longitude, acq_date, acq_time)
)
"""


def map_key() -> Optional[str]:
    return os.environ.get(ENV_KEY) or None


def parse_csv(text: str) -> List[Dict[str, Any]]:
    """Parse a FIRMS area CSV into detection dicts (drops bad rows)."""
    rows: List[Dict[str, Any]] = []
    reader = csv.DictReader(io.StringIO(text))
    for r in reader:
        try:
            lat = float(r["latitude"])
            lon = float(r["longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            continue
        frp = None
        try:
            frp = float(r.get("frp") or 0) or None
        except ValueError:
            frp = None
        rows.append({
            "latitude": lat, "longitude": lon,
            "acq_date": r.get("acq_date"), "acq_time": r.get("acq_time"),
            "satellite": r.get("satellite"),
            "instrument": r.get("instrument"),
            "confidence": str(r.get("confidence") or ""),
            "frp": frp, "daynight": r.get("daynight"),
        })
    return rows


def store_detections(conn: sqlite3.Connection,
                     rows: List[Dict[str, Any]]) -> Dict[str, int]:
    conn.execute(CREATE_SQL)
    now = utc_iso()
    inserted = 0
    for r in rows:
        cur = conn.execute(
            "INSERT OR IGNORE INTO firms_detections "
            "(latitude, longitude, acq_date, acq_time, satellite, instrument,"
            " confidence, frp, daynight, collected_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (r["latitude"], r["longitude"], r["acq_date"], r["acq_time"],
             r["satellite"], r["instrument"], r["confidence"], r["frp"],
             r["daynight"], now))
        inserted += cur.rowcount
    conn.commit()
    return {"inserted": inserted, "seen": len(rows),
            "skipped_duplicate": len(rows) - inserted}


def collect(conn: sqlite3.Connection, session=None, days: int = 1,
            bbox: str = "-180,-90,180,90") -> Dict[str, Any]:
    """Collect FIRMS detections. Disabled without FIRMS_MAP_KEY."""
    key = map_key()
    if not key:
        return {"status": "disabled_no_key",
                "error": "FIRMS_MAP_KEY is not set; register a free key at "
                         "https://firms.modaps.eosdis.nasa.gov/api/area/ "
                         "and add it via the secure vault"}
    session = session or make_session()
    url = FIRMS_URL.format(key=key, bbox=bbox, days=days)
    try:
        resp = session.get(url, timeout=120)
    except Exception as exc:  # noqa: BLE001 - transport failure
        log.warning("firms fetch failed: %s", exc)
        return {"status": "source_unavailable", "error": str(exc)[:200]}
    if resp.status_code != 200:
        return {"status": "source_unavailable",
                "error": "FIRMS HTTP %s" % resp.status_code}
    rows = parse_csv(resp.text)
    stored = store_detections(conn, rows)
    return {"status": "success", "fetched": stored["seen"],
            "inserted": stored["inserted"],
            "skipped_duplicate": stored["skipped_duplicate"]}
