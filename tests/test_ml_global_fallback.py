"""Regression tests for the 2026-10-17 rain_prob retrain failure.

Root cause (verified on the production DB): the time split put every
training row at location 1 (Yangon); the 49 worldwide locations added
2026-10-16 appear only in validation. ``fit_location_aware`` built a
global fallback only from sub-threshold locations, so no global model
existed, validation predictions for the new locations were all NaN,
regression evaluation silently shrank to Yangon, and rain_prob
calibration crashed in IsotonicRegression ("Input X contains NaN").
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ml.weather.calibrate import fit_calibrators  # noqa: E402
from ml.weather.dataset import feature_names  # noqa: E402
from ml.weather.train import (  # noqa: E402
    fit_location_aware,
    predict_with_estimators,
)


def _toy_ds():
    rng = np.random.RandomState(3)
    n1, n2 = 300, 50  # loc 1 in train+val; loc 2 only in validation
    n = n1 + n2
    X = rng.normal(0, 1, size=(n, len(feature_names())))
    y = (rng.rand(n) < 0.3).astype(float)
    ds = SimpleNamespace(
        X=X,
        y={"rain_prob": y},
        location_ids=[1] * n1 + [2] * n2,
        regimes=["clear"] * n,
    )
    tr_idx = np.arange(0, 250)          # only location 1, >= 200 rows
    va_idx = np.arange(250, n)          # loc 1 remainder + all of loc 2
    return ds, tr_idx, va_idx


def test_global_fallback_exists_when_all_train_locations_have_models():
    ds, tr_idx, _ = _toy_ds()
    estimators, handling = fit_location_aware("rain_prob", ds, tr_idx)
    assert 1 in estimators
    assert "global" in estimators, handling


def test_unseen_validation_location_gets_finite_predictions():
    ds, tr_idx, va_idx = _toy_ds()
    estimators, _ = fit_location_aware("rain_prob", ds, tr_idx)
    pred = predict_with_estimators(estimators, ds, va_idx, "rain_prob")
    assert np.isfinite(pred).all()
    assert ((pred >= 0) & (pred <= 1)).all()


def test_calibrators_drop_nan_predictions_instead_of_crashing():
    y = np.array([0.0, 1.0] * 30)
    p = np.linspace(0.05, 0.95, 60)
    p[::7] = np.nan  # predictions missing for some rows
    regimes = ["clear"] * 60
    out = fit_calibrators(y, p, regimes)
    assert out["overall"]["n_dropped_nonfinite"] == 9
    assert out["per_regime"]["clear"]["n"] == 51
