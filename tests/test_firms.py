"""FIRMS adapter tests — fixture only, no network, no key needed.

Covers: CSV parsing (bad rows dropped), dedup on re-store, and the
key-gated disabled path (no key set in the test environment).
"""
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.collectors.disaster import firms  # noqa: E402

FIXTURE_CSV = """latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,instrument,confidence,version,bright_ti5,frp,daynight
16.84,96.17,312.5,1.0,1.0,2026-11-06,0730,Suomi-NPP,VIIRS,nominal,2.0,290.1,12.4,D
-91.0,0.0,300.0,1.0,1.0,2026-11-06,0730,Suomi-NPP,VIIRS,nominal,2.0,290.0,1.0,D
21.5,95.9,305.2,1.0,1.0,2026-11-06,0815,NOAA-20,VIIRS,high,2.0,288.0,4.2,D
"""


def test_parse_csv():
    rows = firms.parse_csv(FIXTURE_CSV)
    assert len(rows) == 2, rows  # the -91 latitude row is dropped
    assert rows[0]["latitude"] == 16.84 and rows[0]["frp"] == 12.4
    assert rows[1]["satellite"] == "NOAA-20"
    assert rows[1]["confidence"] == "high"


def test_store_dedup():
    rows = firms.parse_csv(FIXTURE_CSV)
    conn = sqlite3.connect(":memory:")
    first = firms.store_detections(conn, rows)
    assert first == {"inserted": 2, "seen": 2, "skipped_duplicate": 0}, first
    second = firms.store_detections(conn, rows)
    assert second["inserted"] == 0 and second["skipped_duplicate"] == 2, second


def test_collect_disabled_without_key():
    os.environ.pop("FIRMS_MAP_KEY", None)
    conn = sqlite3.connect(":memory:")
    result = firms.collect(conn)
    assert result["status"] == "disabled_no_key", result
    assert "FIRMS_MAP_KEY" in result["error"]
