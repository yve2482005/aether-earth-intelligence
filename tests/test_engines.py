"""Engine tests (workstream B, spec §§22, 23, 27, 28, 30).

Ensemble math on fixtures, evidence-based (not hard-coded) dynamic
weights, honest-empty cyclone/flood states, and the marine collector
with a mocked HTTP session. All hermetic (tmp DBs), no network.
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.engines import cyclone, dynamic_weights, ensemble, flood, ocean  # noqa: E402

AUX_DDL = """
CREATE TABLE IF NOT EXISTS model_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    location_id INTEGER, variable TEXT NOT NULL, horizon INTEGER NOT NULL,
    model TEXT NOT NULL, mae REAL, rmse REAL, brier REAL,
    sample_count INTEGER, period_start TEXT, period_end TEXT, model_version TEXT
);
CREATE TABLE IF NOT EXISTS earth_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL, start_time TEXT NOT NULL,
    description TEXT
);
CREATE TABLE IF NOT EXISTS system_health (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    component TEXT NOT NULL, status TEXT NOT NULL,
    last_success TEXT, last_failure TEXT,
    latency_ms REAL, error_rate REAL, records INTEGER,
    message TEXT, checked_at TEXT NOT NULL
);
"""


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture()
def aux_db(weather_db):
    conn = sqlite3.connect(weather_db)
    conn.executescript(AUX_DDL)
    conn.commit()
    conn.close()
    return weather_db


def _conn(db_path: str) -> sqlite3.Connection:
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    return c


def _four_models(db_path: str, base: datetime):
    """One valid_time, four models with known temperatures 20/22/24/26."""
    conn = sqlite3.connect(db_path)
    vt = _iso(base)
    for i, m in enumerate(["gfs_seamless", "icon_seamless", "ecmwf_ifs", "jma_seamless"]):
        conn.execute(
            """INSERT INTO weather_forecasts
               (location_id, source, model, forecast_run, forecast_hour,
                issued_at, valid_time, temperature, humidity, pressure,
                wind_speed, precipitation, precip_probability, cloud_cover,
                collected_at)
               VALUES (1,'open-meteo',?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (m, vt, 0, vt, vt, 20.0 + 2 * i, 70.0, 1008.0,
             3.0, 0.0, 0.1, 20.0, vt))
    conn.commit()
    conn.close()


def test_ensemble_math_mean_median_quantiles_spread(aux_db):
    base = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    _four_models(aux_db, base)
    out = ensemble.ensemble_for_location(_conn(aux_db), 1, hours=48)
    assert out["kind"] == "MODEL OUTPUT"
    assert len(out["items"]) == 1
    t = out["items"][0]["variables"]["temperature"]
    assert t["mean"] == pytest.approx(23.0)
    assert t["median"] == pytest.approx(23.0)
    assert t["min"] == pytest.approx(20.0) and t["max"] == pytest.approx(26.0)
    # spread must be > 0 with genuinely different models
    assert t["spread"] == pytest.approx(6.0)
    assert t["q25"] == pytest.approx(20.5)
    assert t["q75"] == pytest.approx(25.5)
    assert t["n"] == 4
    assert out["items"][0]["n_models"] == 4


def test_weights_no_evidence_means_equal_not_hardcoded(aux_db):
    out = dynamic_weights.weights_for(_conn(aux_db), 1, "temperature", 6)
    assert out["evidence"] == "insufficient"
    ws = out["weights"]
    assert len(ws) == 4
    assert all(v == pytest.approx(0.25) for v in ws.values())
    assert abs(sum(ws.values()) - 1.0) < 1e-6
    # equal weights must hold for ANY geography/variable/horizon, never per-model
    for loc, var, h in [(2, "wind_speed", 24), (1, "precipitation", 72)]:
        o = dynamic_weights.weights_for(_conn(aux_db), loc, var, h)
        assert o["evidence"] == "insufficient"
        assert all(v == pytest.approx(0.25) for v in o["weights"].values())
    assert "never hard-coded" in out["note"].lower()


def test_weights_follow_evidence_inverse_error(aux_db):
    conn = sqlite3.connect(aux_db)
    # gfs twice as accurate as icon -> should get ~2x the weight
    rows = [("gfs_seamless", 1.0), ("icon_seamless", 2.0),
            ("ecmwf_ifs", 4.0), ("jma_seamless", 8.0)]
    for m, rmse in rows:
        conn.execute(
            """INSERT INTO model_scores
               (location_id, variable, horizon, model, rmse, sample_count,
                period_start, period_end, model_version)
               VALUES (1,'temperature',6,?,?,30,'2026-10-01T00:00:00Z',
                       '2026-10-05T00:00:00Z','v1')""", (m, rmse))
    conn.commit()
    conn.close()
    out = dynamic_weights.weights_for(_conn(aux_db), 1, "temperature", 6)
    assert out["evidence"] == "model_scores"
    ws = out["weights"]
    assert abs(sum(ws.values()) - 1.0) < 1e-6
    # 1/rmse normalization: w_gfs / w_icon == 2, w_gfs / w_ecmwf == 4
    # (rounded to 4dp in the engine, so compare with a small tolerance)
    assert ws["gfs_seamless"] / ws["icon_seamless"] == pytest.approx(2.0, rel=1e-3)
    assert ws["gfs_seamless"] / ws["ecmwf_ifs"] == pytest.approx(4.0, rel=1e-3)
    assert ws["jma_seamless"] < ws["ecmwf_ifs"] < ws["icon_seamless"] < ws["gfs_seamless"]
    # weights change when evidence changes -> not hard-coded
    assert out["note"].lower().startswith("weights proportional")


def test_weights_below_sample_threshold_ignored(aux_db):
    conn = sqlite3.connect(aux_db)
    conn.execute(
        """INSERT INTO model_scores
           (location_id, variable, horizon, model, rmse, sample_count)
           VALUES (1,'temperature',6,'gfs_seamless',0.5,3)""")
    conn.commit()
    conn.close()
    out = dynamic_weights.weights_for(_conn(aux_db), 1, "temperature", 6)
    # 3 samples < MIN_SAMPLES (30): evidence ignored -> equal weights
    assert out["evidence"] == "insufficient"
    assert all(v == pytest.approx(0.25) for v in out["weights"].values())


def test_cyclone_honest_empty_state(aux_db):
    out = cyclone.status(_conn(aux_db), days=14)
    assert out["status"] == "no_active_cyclone_data_source_configured"
    assert out["events"] == []
    assert "no" in (out.get("message") or "").lower() or out["status"].startswith("no_active")


def test_flood_honest_cannot_assess(aux_db):
    out = flood.assess(_conn(aux_db), 1, hours=72)
    assert out["risk"]["status"] == "cannot_assess"
    assert out["risk"]["kind"] == "RISK"
    assert out["exposure"]["status"] == "insufficient_data"
    # never labels an area flooding/at-risk/safe
    blob = " ".join([out["risk"].get("message", ""), str(out["hazard"].get("status"))])
    assert "NOT labeled" in blob or "not labeled" in blob.lower()
    assert out["kind"] == "RISK"


def test_flood_rainfall_forecast_only_with_data(aux_db):
    base = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    _four_models(aux_db, base)
    out = flood.assess(_conn(aux_db), 1, hours=72)
    assert out["hazard"]["status"] == "rainfall_forecast_only"
    assert out["hazard"]["kind"] == "FORECAST"
    assert out["risk"]["status"] == "cannot_assess"  # rainfall alone != flood


class _FakeResp:
    def __init__(self, data):
        self._data = data
        self.status_code = 200
        self.url = "https://marine-api.open-meteo.com/v1/marine"

    def json(self):
        return self._data

    def raise_for_status(self):
        pass


class _FakeSession:
    def __init__(self, payload):
        self._payload = payload

    def get(self, url, params=None, timeout=None):
        return _FakeResp(self._payload)


def test_marine_collector_mocked(aux_db):
    from backend.db.migrate import ensure_v2
    from datetime import timedelta
    conn = _conn(aux_db)
    ensure_v2(conn)
    base = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    t1 = (base + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
    t2 = (base + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")
    session = _FakeSession({
        "hourly": {
            "time": [t1, t2],
            "wave_height": [1.2, 1.5],
            "wave_period": [8.1, 8.4],
            "wave_direction": [180.0, 185.0],
            "sea_surface_temperature": [28.5, 28.4],
        }
    })
    out = ocean.collect(conn, 1, 16.8, 96.15, session=session)
    assert out["ok"] is True
    assert out["inserted"] == 2
    latest = ocean.latest(conn, 1)
    assert latest["count"] == 2
    assert len(latest["items"]) == 2
    assert latest["items"][0]["wave_height"] == pytest.approx(1.2)
    assert latest["items"][0]["kind"] == "FORECAST"
    assert latest["units"]["wave_height"] == "m"
    assert latest["provenance"]["sources"] == ["open-meteo-marine"]
    conn.close()


def test_marine_collector_http_failure_never_raises(aux_db):
    from backend.db.migrate import ensure_v2
    conn = _conn(aux_db)
    ensure_v2(conn)

    class _BadSession:
        def get(self, *a, **k):
            raise ConnectionError("offline")

    out = ocean.collect(conn, 1, 16.8, 96.15, session=_BadSession())
    assert out["ok"] is False
    assert out["error"]
    conn.close()
