"""Time-series correctness tests.

* walk-forward folds: evaluation windows never overlap and never precede
  their training cutoff ("pretend it's date X, only use data <= X").
* metric functions return sane values on synthetic data:
  MAE >= 0, RMSE >= MAE, Brier in [0,1], ECE in [0,1], CRPS >= 0.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from helpers import add_forecast, add_observation, iso  # noqa: E402
from ml.evaluation.backtest import make_cutoffs, walk_forward  # noqa: E402
from ml.weather.evaluate import (  # noqa: E402
    probability_metrics,
    regression_metrics,
)
from ml.weather.leakage import parse_ts  # noqa: E402


def _rng(seed=0):
    return np.random.RandomState(seed)


# ---------------------------------------------------------------- metrics

def test_regression_metrics_sane():
    rng = _rng()
    y = rng.normal(30, 5, size=500)
    p = y + rng.normal(0, 2, size=500)
    m = regression_metrics(y, p)
    assert m["n"] == 500
    assert m["mae"] >= 0
    assert m["rmse"] >= m["mae"] - 1e-9  # RMSE >= MAE by Jensen
    assert m["crps"] >= 0
    assert m["crps"] <= m["rmse"] + 1e-9  # CRPS <= RMSE for same sigma


def test_regression_metrics_perfect():
    y = np.array([1.0, 2.0, 3.0])
    m = regression_metrics(y, y)
    assert m["mae"] == pytest.approx(0.0)
    assert m["rmse"] == pytest.approx(0.0)


def test_regression_metrics_empty():
    m = regression_metrics(np.array([]), np.array([]))
    assert m["n"] == 0 and m["mae"] is None


def test_probability_metrics_sane():
    rng = _rng(1)
    y = (rng.rand(600) < 0.3).astype(float)
    p = np.clip(0.3 + rng.normal(0, 0.15, size=600), 0.01, 0.99)
    m = probability_metrics(y, p)
    assert m["n"] == 600
    assert 0.0 <= m["brier"] <= 1.0
    assert 0.0 <= m["calibration_error"] <= 1.0
    # reliability bins cover [0,1] and count every sample exactly once
    bins = m["reliability_bins"]
    assert len(bins) == 10
    assert sum(b["n"] for b in bins) == 600
    assert bins[0]["bin_low"] == 0.0 and bins[-1]["bin_high"] == 1.0


def test_probability_metrics_perfect():
    y = np.array([0.0, 1.0, 0.0, 1.0])
    p = np.array([0.0, 1.0, 0.0, 1.0])
    m = probability_metrics(y, p)
    assert m["brier"] == pytest.approx(0.0)
    assert m["calibration_error"] == pytest.approx(0.0)


def test_brier_worst_case_bounded():
    y = np.array([0.0, 0.0, 1.0, 1.0])
    p = np.array([1.0, 1.0, 0.0, 0.0])  # maximally wrong
    m = probability_metrics(y, p)
    assert m["brier"] == pytest.approx(1.0)


# ------------------------------------------------------------ walk-forward

def test_make_cutoffs_monotonic_and_non_overlapping():
    cuts = make_cutoffs("2026-09-01T00:00:00Z", "2026-09-22T00:00:00Z", 7)
    assert cuts == ["2026-09-01T00:00:00Z", "2026-09-08T00:00:00Z",
                    "2026-09-15T00:00:00Z"]
    pts = [parse_ts(c) for c in cuts]
    assert all(b > a for a, b in zip(pts, pts[1:]))


def _dense_db(tmp_path):
    """20 days of hourly forecasts+observations at one location."""
    import sqlite3

    from helpers import WEATHER_DDL  # noqa: E402

    db = str(tmp_path / "wf.db")
    conn = sqlite3.connect(db)
    conn.executescript(WEATHER_DDL)
    conn.execute(
        "INSERT INTO locations (id, name, latitude, longitude, created_at)"
        " VALUES (1,'W',16.8,96.15,'2026-01-01T00:00:00Z')")
    conn.commit()
    conn.close()
    base = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
    rng = _rng(7)
    for h in range(20 * 24):
        vt = iso(base + timedelta(hours=h))
        ft = 30 + 3 * np.sin(2 * np.pi * (h % 24) / 24) + rng.normal(0, 0.5)
        add_forecast(db, vt, fh=h % 24, temp=float(ft))
        add_observation(db, vt, temp=float(ft + rng.normal(0, 1.0)))
    return db


def test_walk_forward_windows_do_not_overlap(tmp_path):
    db = _dense_db(tmp_path)
    folds = walk_forward(db, "temperature", "2026-09-10T00:00:00Z",
                         "2026-09-20T00:00:00Z", step_days=5)
    assert len(folds) >= 2
    for f in folds:
        assert f["status"] == "ok", f["status"]
        # eval window strictly after the training cutoff
        assert parse_ts(f["eval_start"]) == parse_ts(f["train_end"])
        assert parse_ts(f["eval_end"]) > parse_ts(f["eval_start"])
    # consecutive eval windows are disjoint and ordered
    for a, b in zip(folds, folds[1:]):
        assert parse_ts(b["eval_start"]) >= parse_ts(a["eval_end"])
        assert parse_ts(b["train_end"]) > parse_ts(a["train_end"])


def test_walk_forward_metrics_sane(tmp_path):
    db = _dense_db(tmp_path)
    folds = walk_forward(db, "temperature", "2026-09-10T00:00:00Z",
                         "2026-09-17T00:00:00Z", step_days=7)
    ok = [f for f in folds if f["status"] == "ok"]
    assert ok, "expected at least one successful fold"
    for f in ok:
        assert f["eval_mae"] >= 0
        assert f["eval_rmse"] >= f["eval_mae"] - 1e-9
        assert f["eval_n"] >= 10 if "eval_n" in f else True
