"""ERA5 archive backfill tests (2026-11-06).

Tier B locations collected zero nowcast observations (fan-out bug), so
their accuracy was unmeasurable. The backfill stores ERA5 reanalysis
(source='era5-archive', quality='reanalysis') — never mistaken for a
live nowcast or a station reading.
"""

import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
ROOT = os.path.join(os.path.dirname(__file__), "..")

from backend.collectors.weather import archive_backfill  # noqa: E402


def _memdb():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    with open(os.path.join(ROOT, "backend", "db", "schema.sql"),
              encoding="utf-8") as fh:
        conn.executescript(fh.read())
    return conn


def _hourly():
    return {
        "time": ["2026-11-05T00:00", "2026-11-05T01:00",
                 "2026-11-05T02:00", "2026-11-05T03:00"],
        "temperature_2m": [12.3, None, 200.0, 15.5],
        "relative_humidity_2m": [96, 91, 82, 80],
        "pressure_msl": [1023.5, 1023.9, 1023.8, 1024.0],
        "wind_speed_10m": [0.53, 0.35, 0.32, 0.4],
        "wind_direction_10m": [180, 190, 200, 210],
        "precipitation": [0.0, 0.0, 0.0, 0.1],
    }


def test_store_archive_labels_source_and_skips_bad_rows():
    conn = _memdb()
    out = archive_backfill.store_archive(conn, 56, _hourly())
    # Hour 0 stored; hour 1 null temp skipped; hour 2 (200C) skipped;
    # hour 3 stored.
    assert out == {"inserted": 2, "skipped": 2, "errors": 0}
    rows = conn.execute(
        "SELECT observed_at, temperature, source, quality "
        "FROM weather_observations ORDER BY observed_at").fetchall()
    assert [r["observed_at"] for r in rows] == [
        "2026-11-05T00:00:00Z", "2026-11-05T03:00:00Z"]
    assert all(r["source"] == "era5-archive" for r in rows)
    assert all(r["quality"] == "reanalysis" for r in rows)
    assert rows[0]["temperature"] == 12.3


def test_store_archive_rerun_is_idempotent():
    conn = _memdb()
    assert archive_backfill.store_archive(conn, 56, _hourly())["inserted"] == 2
    out = archive_backfill.store_archive(conn, 56, _hourly())
    assert out["inserted"] == 0
    assert conn.execute("SELECT COUNT(*) FROM weather_observations"
                        ).fetchone()[0] == 2


class _FakeResp:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload
        self.url = "https://example.invalid/archive"

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, payload):
        self._payload = payload

    def get(self, url, params=None, timeout=None):
        return _FakeResp(self._payload)


def test_fetch_archive_parses_hourly():
    payload = {"hourly": {"time": ["2026-11-05T00:00"],
                          "temperature_2m": [12.3]}}
    res = archive_backfill.fetch_archive(
        _FakeSession(payload), 30.58, 114.27, "2026-11-05", "2026-11-05")
    assert res["ok"] is True
    assert res["hourly"]["time"] == ["2026-11-05T00:00"]
