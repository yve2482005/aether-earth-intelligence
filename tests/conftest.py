"""Shared pytest fixtures for the AETHER ML test suite."""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests/helpers.py
from helpers import WEATHER_DDL, add_forecast, add_observation, iso  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

@pytest.fixture()
def weather_db(tmp_path):
    """Empty weather DB (forecasts + observations + locations tables)."""
    db = str(tmp_path / "t.db")
    conn = sqlite3.connect(db)
    conn.executescript(WEATHER_DDL)
    conn.execute(
        "INSERT INTO locations (id, name, latitude, longitude, created_at)"
        " VALUES (1, 'Testville', 16.8, 96.15, '2026-01-01T00:00:00Z')")
    conn.commit()
    conn.close()
    yield db


@pytest.fixture()
def populated_weather_db(weather_db):
    """48h of hourly forecasts+observations; one deliberately late obs."""
    base = datetime(2026, 9, 20, 0, 0, tzinfo=timezone.utc)
    for h in range(48):
        vt = iso(base + timedelta(hours=h))
        add_forecast(weather_db, vt, fh=h, temp=30.0 - 0.05 * h)
        add_observation(weather_db, vt, temp=29.5 - 0.05 * h)
    # one observation 3h AFTER its forecast valid_time -> must be dropped
    late_vt = iso(base + timedelta(hours=60))
    add_forecast(weather_db, late_vt, fh=60)
    add_observation(weather_db,
                    iso(base + timedelta(hours=63)), temp=29.0)
    return weather_db


# --- acceptance harness ordering guards (workstream F, additive) ---------------
# Root cause (verified): the category tests append verdicts to
# tests/.acceptance_results.jsonl during the run while
# test_acceptance_report.py reads that file. If the reader ever runs before
# the writers (shuffled order, -k subsets, etc.) it sees missing/stale
# verdicts. Guards:
#   1. pytest_sessionstart: reset the results file so stale verdicts from an
#      earlier (possibly load-affected) run can never contaminate this one.
#   2. pytest_collection_modifyitems: force test_acceptance_report.py to run
#      last in the session, after every writer, regardless of order plugins.
def pytest_sessionstart(session):
    try:
        from tests.test_acceptance_lib import reset_results
    except ImportError:
        try:
            from test_acceptance_lib import reset_results
        except ImportError:
            return
    reset_results()


def pytest_collection_modifyitems(session, config, items):
    writers = [i for i in items if "test_acceptance_report" not in i.nodeid]
    readers = [i for i in items if "test_acceptance_report" in i.nodeid]
    if readers:
        items[:] = writers + readers
