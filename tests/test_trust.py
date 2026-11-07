"""Tests for the trust workstream: trust index bounds/labels, lineage chain
completeness, data-quality honesty, verification metrics, health mapping.

Uses throwaway SQLite DBs — never the production aether.db.
"""
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

from backend.api.routes.trust import map_health  # noqa: E402
from backend.trust import (  # noqa: E402
    build_lineage_chain,
    compute_metrics,
    compute_trust_index,
    dataset_quality,
)
from backend.trust import quality as qmod  # noqa: E402
from backend.trust.verification import VerificationError, baseline_scores  # noqa: E402

SUPPORT_DDL = """
CREATE TABLE source_registry (
    source_id TEXT PRIMARY KEY, provider TEXT NOT NULL, dataset TEXT NOT NULL,
    category TEXT NOT NULL, url TEXT, api_type TEXT, update_frequency TEXT,
    geographic_coverage TEXT, resolution TEXT, variables TEXT, license TEXT,
    attribution TEXT, commercial_use TEXT, redistribution TEXT, rate_limits TEXT,
    auth_required INTEGER DEFAULT 0, priority INTEGER DEFAULT 5,
    reliability_score REAL, last_success TEXT, last_failure TEXT,
    health TEXT DEFAULT 'unknown', notes TEXT);
CREATE TABLE model_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT, location_id INTEGER,
    variable TEXT NOT NULL, horizon INTEGER NOT NULL, model TEXT NOT NULL,
    mae REAL, rmse REAL, brier REAL, crps REAL, calibration_error REAL,
    sample_count INTEGER, period_start TEXT, period_end TEXT,
    model_version TEXT);
CREATE TABLE model_versions (
    version TEXT PRIMARY KEY, task TEXT NOT NULL, dataset_version TEXT,
    feature_version TEXT, source_versions TEXT, training_start TEXT,
    training_end TEXT, validation_start TEXT, validation_end TEXT,
    metrics TEXT, hyperparameters TEXT, git_commit TEXT, deployed_at TEXT,
    is_champion INTEGER DEFAULT 0);
"""


def ro(path):
    """Open a connection with dict-like rows (as backend.db.connect does)."""
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    return c


@pytest.fixture()
def db(tmp_path):
    path = str(tmp_path / "trust.db")
    conn = sqlite3.connect(path)
    conn.executescript(WEATHER_DDL + SUPPORT_DDL)
    conn.execute(
        "INSERT INTO locations (id, name, latitude, longitude, created_at)"
        " VALUES (1, 'Testville', 16.8, 96.15, '2026-01-01T00:00:00Z')")
    conn.commit()
    conn.close()
    yield path


def _register(conn, age_hours=0.5, health="ok", reliability=0.9, base=None):
    ts = ((base or datetime.now(timezone.utc)) - timedelta(hours=age_hours)
          ).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        """INSERT INTO source_registry
           (source_id, provider, dataset, category, url, geographic_coverage,
            license, attribution, commercial_use, reliability_score,
            last_success, health)
           VALUES ('open-meteo-forecast', 'Open-Meteo', 'forecast', 'weather',
                   'https://api.open-meteo.com/v1/forecast', 'global',
                   'CC-BY-4.0', 'Weather data by Open-Meteo.com',
                   'non-commercial', ?, ?, ?)""",
        (reliability, ts, health))


# ---------------- trust index ----------------

def test_trust_index_bounds_and_label(db):
    base = datetime(2026, 9, 20, 0, 0, tzinfo=timezone.utc)
    for h in range(48):
        vt = iso(base + timedelta(hours=h))
        add_forecast(db, vt, fh=h, temp=30.0 - 0.05 * h)
        add_observation(db, vt, temp=29.5 - 0.05 * h)
    # two models for the agreement component
    conn = ro(db)
    _register(conn, age_hours=0.25)
    conn.commit()
    conn.close()
    conn = ro(db)
    idx = compute_trust_index(conn, 1)
    conn.close()
    assert 0.0 <= idx["trust_index"] <= 100.0
    assert idx["label"] == "transparency indicator, not a correctness guarantee"
    for k, v in idx["components"].items():
        assert 0.0 <= v <= 1.0, k
    assert "disclaimer" in idx and "NOT a correctness guarantee" in idx["disclaimer"]


# ---------------- lineage ----------------

def test_lineage_chain_complete(db):
    conn = ro(db)
    _register(conn)
    conn.execute("""INSERT INTO model_versions
        (version, task, deployed_at, is_champion)
        VALUES ('weather_temperature_v1', 'temperature',
                '2026-09-20T00:00:00Z', 1)""")
    conn.commit()
    conn.close()
    base = datetime(2026, 9, 20, 0, 0, tzinfo=timezone.utc)
    add_forecast(db, iso(base), fh=0, temp=30.0)
    conn = ro(db)
    chain = build_lineage_chain(conn, "weather-forecast", location_id=1,
                                model="gfs", valid_time=iso(base))
    conn.close()
    levels = [n["level"] for n in chain["chain"]]
    assert levels == ["ui-result", "model-version", "processed-dataset",
                      "raw-data", "source"]
    assert chain["complete"] is True
    src = chain["chain"][-1]["fields"]
    assert src["source"] == "open-meteo-forecast"
    assert src["url"] == "https://api.open-meteo.com/v1/forecast"
    assert src["license"] == "CC-BY-4.0"
    raw = chain["chain"][3]["fields"]
    assert raw["retrieval_timestamp"] and raw["source_timestamp"]
    assert chain["chain"][2]["fields"]["processing_timestamp"]


def test_lineage_missing_hop_is_honest(db):
    # no source_registry entry -> source node marked missing, never fabricated
    base = datetime(2026, 9, 20, 0, 0, tzinfo=timezone.utc)
    add_forecast(db, iso(base), fh=0, temp=30.0)
    conn = ro(db)
    chain = build_lineage_chain(conn, "weather-forecast", location_id=1,
                                model="gfs", valid_time=iso(base))
    conn.close()
    assert chain["complete"] is False
    assert chain["chain"][-1]["missing"] is True


def test_lineage_quake_chain(db):
    conn = ro(db)
    conn.execute("""CREATE TABLE earthquake_events (
        canonical_event_id TEXT PRIMARY KEY, source TEXT NOT NULL,
        source_event_ids TEXT NOT NULL, origin_time TEXT NOT NULL,
        latitude REAL NOT NULL, longitude REAL NOT NULL, depth REAL,
        magnitude REAL, magnitude_type TEXT, region TEXT, status TEXT,
        review_status TEXT, felt_count INTEGER, mmi REAL, alert_level TEXT,
        tsunami_flag INTEGER DEFAULT 0, shakemap_available INTEGER DEFAULT 0,
        pager_available INTEGER DEFAULT 0, source_url TEXT, update_time TEXT,
        created_at TEXT NOT NULL)""")
    conn.execute("""INSERT INTO earthquake_events
        (canonical_event_id, source, source_event_ids, origin_time, latitude,
         longitude, depth, magnitude, magnitude_type, region, created_at)
        VALUES ('usgs-1', 'usgs', '["a"]', '2026-09-20T00:00:00Z', 16.8, 96.15,
                10.0, 6.1, 'mw', 'Test Region', '2026-09-20T00:05:00Z')""")
    conn.execute("""INSERT INTO source_registry
        (source_id, provider, dataset, category, url, license)
        VALUES ('usgs-earthquake-all-hour', 'USGS', 'earthquakes', 'earthquake',
                'https://earthquake.usgs.gov/fdsnws/event/1/', 'public domain')""")
    conn.commit()
    chain = build_lineage_chain(conn, "quake", canonical_event_id="usgs-1")
    conn.close()
    assert chain["complete"] is True
    assert chain["chain"][-1]["fields"]["license"] == "public domain"


# ---------------- quality honesty ----------------

def test_quality_stale_data_scores_low_freshness(db, monkeypatch):
    now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(qmod, "_SCORE_NOW", now)
    conn = ro(db)
    _register(conn, age_hours=24 * 10, base=now)  # 10 days stale
    conn.execute(
        """INSERT INTO weather_observations (location_id, station, observed_at,
           temperature, humidity, pressure, wind_speed, precipitation, source,
           quality, collected_at)
           VALUES (1, 'T', ?, 29.5, 72.0, 1007.0, 3.5, 0.0, 'open-meteo', 'ok', ?)""",
        (iso(now - timedelta(days=10)),) * 2)
    conn.commit()
    q = dataset_quality(conn, "weather-observations")
    conn.close()
    assert q["components"]["freshness"] <= 0.2, q
    assert q["label"] in ("Poor", "Weak"), q


def test_quality_fresh_data_scores_high(db, monkeypatch):
    now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(qmod, "_SCORE_NOW", now)
    conn = ro(db)
    _register(conn, age_hours=0.1, base=now)
    for h in range(24):
        t = iso(now - timedelta(hours=h))
        conn.execute(
            """INSERT INTO weather_observations (location_id, station, observed_at,
               temperature, humidity, pressure, wind_speed, precipitation, source,
               quality, collected_at)
               VALUES (1, 'T', ?, 29.5, 72.0, 1007.0, 3.5, 0.0, 'open-meteo', 'ok', ?)""",
            (t, t))
    conn.commit()
    q = dataset_quality(conn, "weather-observations")
    conn.close()
    assert q["components"]["freshness"] == 1.0
    assert q["components"]["validity"] == 1.0
    assert q["score"] >= 85, q


# ---------------- health mapping ----------------

@pytest.mark.parametrize("health,age_h,expected", [
    ("ok", 0.5, "HEALTHY"),
    ("ok", 30, "STALE"),
    ("degraded", 0.5, "DEGRADED"),
    ("down", 0.5, "FAILED"),
    ("unavailable", 0.5, "DISABLED"),
    ("unknown", 0.5, "HEALTHY"),
    ("unknown", 72, "STALE"),
])
def test_health_mapping(health, age_h, expected):
    ts = (datetime.now(timezone.utc) - timedelta(hours=age_h)
          ).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert map_health(health, ts)["status"] == expected


# ---------------- verification metrics ----------------

def test_compute_metrics_known_values():
    m = compute_metrics([(30.0, 29.0), (31.0, 30.0), (29.0, 30.0)])
    assert m["sample_count"] == 3
    assert abs(m["mae"] - 1.0) < 1e-9
    assert abs(m["bias"] - (1 / 3)) < 1e-9
    assert abs(m["crps"] - m["mae"]) < 1e-12
    assert "deterministic" in m["crps_note"]


def test_compute_metrics_no_pairs_raises():
    with pytest.raises(VerificationError):
        compute_metrics([])


def test_brier_and_reliability():
    probs = [((0.8 if e else 0.2), e) for e in [1, 1, 1, 0, 0, 0]]
    m = compute_metrics([(1, 1)] * 6, probabilities=probs)
    assert 0.0 <= m["brier"] <= 1.0
    assert m["calibration_error"] is not None
    assert len(m["reliability"]["bins"]) == 10


def test_baseline_scores_from_observations(db):
    # Base date is relative to now: baseline_scores only reads the trailing
    # `days` (30) from now, so a fixed 2026-09-20 base ages out of the
    # window and the call raises "not enough to build honest baselines"
    # (failed 2026-11-07 as a time-bomb).
    base = datetime.now(timezone.utc).replace(
        minute=0, second=0, microsecond=0) - timedelta(days=3)
    for h in range(72):
        vt = iso(base + timedelta(hours=h))
        add_observation(db, vt, temp=30.0 + 2.0 * (1 if h % 24 < 12 else -1))
    conn = ro(db)
    out = baseline_scores(conn, 1, "temperature", 24, days=30)
    conn.close()
    assert "persistence" in out["baselines"]
    assert "climatology" in out["baselines"]
    assert out["baselines"]["persistence"]["sample_count"] > 0
