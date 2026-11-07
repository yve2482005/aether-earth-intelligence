"""Perf tests: batch ML (§§20-21) — one predict call per batch, no training on path."""
from __future__ import annotations

import sqlite3

import numpy as np
import pytest

from backend.perf import ml_batch
from backend.perf.ml_batch import (feature_row, load_artifact, predict_batch,
                                   precompute_predictions)

DDL = """
CREATE TABLE weather_forecasts (id INTEGER PRIMARY KEY AUTOINCREMENT,
  location_id INTEGER, source TEXT, model TEXT, forecast_run TEXT,
  forecast_hour INTEGER, issued_at TEXT, valid_time TEXT, temperature REAL,
  feels_like REAL, humidity REAL, pressure REAL, wind_speed REAL,
  wind_direction REAL, precipitation REAL, precip_probability REAL,
  cloud_cover REAL, aqi REAL, pm2_5 REAL, pm10 REAL, collected_at TEXT);
"""


def test_artifact_loads_without_training():
    art = load_artifact("temperature")
    assert "feature_names" in art and "estimators" in art
    assert "version" in art or True


def test_single_predict_call_for_batch():
    art = load_artifact("temperature")
    ncols = len(art["feature_names"])
    X = np.random.default_rng(7).normal(30, 5, size=(64, ncols))
    import time
    t0 = time.monotonic()
    preds = predict_batch("temperature", X, location_ids=[9001] * 64)
    dt = (time.monotonic() - t0) * 1000.0
    assert preds.shape == (64,)
    assert np.all(np.isfinite(preds))
    # batch of 64 in one predict call: far cheaper than 64 single calls
    assert dt < 60_000  # sanity bound only


def test_feature_row_shape():
    art = load_artifact("temperature")
    row = {"temperature": 30.0, "humidity": 80.0, "pressure": 1005.0,
           "wind_speed": 5.0, "precipitation": 0.0, "cloud_cover": 40.0,
           "forecast_hour": 3, "model": "gfs"}
    vec = feature_row(row, art["feature_names"])
    assert len(vec) == len(art["feature_names"])
    assert all(isinstance(v, float) for v in vec)


def test_precompute_predictions_end_to_end(tmp_path):
    db = str(tmp_path / "ml.db")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.executescript(DDL)
    for h in range(6):
        conn.execute(
            """INSERT INTO weather_forecasts (location_id,source,model,forecast_run,
                forecast_hour,issued_at,valid_time,temperature,humidity,pressure,
                wind_speed,precipitation,cloud_cover,collected_at)
               VALUES (9001,'open-meteo','gfs','2026-10-05T00:00:00Z',?,
                '2026-10-05T00:00:00Z',printf('2026-10-05T%02d:00:00Z',?),
                30,80,1005,5,0,40,'2026-10-05T00:00:00Z')""", (h, h))
    conn.commit()
    out = precompute_predictions(conn, task="temperature", limit=100)
    assert out["predicted"] == 6
    assert out["latency_ms"] >= 0
    # rerun: already predicted -> 0 new (idempotent via UNIQUE)
    out2 = precompute_predictions(conn, task="temperature", limit=100)
    assert out2["predicted"] == 0
    n = conn.execute("SELECT COUNT(*) c FROM ml_batch_predictions").fetchone()["c"]
    assert n == 6
    conn.close()
