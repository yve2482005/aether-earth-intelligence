"""Probability calibration for the rain_probability task.

Fits, per weather regime, two calibrators on held-out validation data:
  * Isotonic regression (non-parametric)
  * Platt scaling (logistic / sigmoid)

The method with the lower validation Brier score is kept per regime.
Reports Brier before/after, calibration error (ECE), and a reliability-bin
table so calibration quality is inspectable, not just a single number.

Calibrated probabilities express data confidence (agreement between past
predicted probabilities and observed frequencies). They are NOT certainty
about future rainfall.
"""

from __future__ import annotations

import numpy as np
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression

from ml.calibration.utils import (
    brier_score,
    expected_calibration_error,
    reliability_table,
)
from ml.weather.dataset import REGIMES


def _fit_platt(p: np.ndarray, y: np.ndarray) -> LogisticRegression:
    lr = LogisticRegression(max_iter=1000)
    lr.fit(p.reshape(-1, 1), y)
    return lr


def fit_calibrators(y_true: np.ndarray, y_prob: np.ndarray,
                    regimes: list[str]) -> dict:
    """Fit per-regime calibrators. Returns a report dict (JSON-serializable
    parts) plus the fitted objects under ``'_fitted'``."""
    y_true = np.asarray(y_true, dtype=float)
    y_prob = np.asarray(y_prob, dtype=float)
    regimes = list(regimes)
    # Defensive: a prediction can be NaN when a validation location has
    # no estimator. Calibration must drop those pairs (and report how
    # many) instead of crashing inside IsotonicRegression — the
    # 2026-10-17 rain_prob retrain failed exactly that way.
    finite = np.isfinite(y_true) & np.isfinite(y_prob)
    n_dropped_nonfinite = int((~finite).sum())
    if n_dropped_nonfinite:
        y_true, y_prob = y_true[finite], y_prob[finite]
        regimes = [r for r, keep in zip(regimes, finite) if keep]
    y_prob = np.clip(y_prob, 1e-6, 1 - 1e-6)

    fitted: dict[str, dict] = {}
    report: dict[str, dict] = {}
    for regime in REGIMES:
        mask = np.array([r == regime for r in regimes])
        n = int(mask.sum())
        entry: dict = {"n": n}
        if n < 20 or len(np.unique(y_true[mask])) < 2:
            entry.update({
                "method": "none",
                "reason": "too few samples or single class; "
                          "uncalibrated probabilities used",
                "brier_before": round(brier_score(y_true[mask], y_prob[mask]), 5)
                if n else None,
            })
            fitted[regime] = {"method": "none"}
            report[regime] = entry
            continue

        yt, yp = y_true[mask], y_prob[mask]
        iso = IsotonicRegression(out_of_bounds="clip")
        iso_p = iso.fit_transform(yp, yt)
        platt = _fit_platt(yp, yt)
        platt_p = platt.predict_proba(yp.reshape(-1, 1))[:, 1]

        b_before = brier_score(yt, yp)
        b_iso = brier_score(yt, iso_p)
        b_platt = brier_score(yt, platt_p)
        if b_iso <= b_platt:
            method, cal_p, cal = "isotonic", iso_p, iso
        else:
            method, cal_p, cal = "platt", platt_p, platt

        fitted[regime] = {"method": method, "calibrator": cal}
        entry.update({
            "method": method,
            "brier_before": round(b_before, 5),
            "brier_after": round(brier_score(yt, cal_p), 5),
            "brier_isotonic": round(b_iso, 5),
            "brier_platt": round(b_platt, 5),
            "calibration_error_before": round(
                expected_calibration_error(yt, yp), 5),
            "calibration_error_after": round(
                expected_calibration_error(yt, cal_p), 5),
            "reliability_bins": reliability_table(yt, cal_p),
        })
        report[regime] = entry

    overall = {
        "brier_before": round(brier_score(y_true, y_prob), 5),
        "calibration_error_before": round(
            expected_calibration_error(y_true, y_prob), 5),
        "n_dropped_nonfinite": n_dropped_nonfinite,
    }
    return {"per_regime": report, "overall": overall, "_fitted": fitted}


def apply_calibrators(fitted: dict, y_prob: np.ndarray,
                      regimes: list[str]) -> np.ndarray:
    """Apply fitted per-regime calibrators to new probabilities."""
    y_prob = np.asarray(y_prob, dtype=float)
    out = y_prob.copy()
    for regime, spec in fitted.items():
        mask = np.array([r == regime for r in regimes])
        if not mask.any():
            continue
        method = spec.get("method", "none")
        if method == "isotonic":
            out[mask] = spec["calibrator"].transform(
                np.clip(y_prob[mask], 1e-6, 1 - 1e-6))
        elif method == "platt":
            out[mask] = spec["calibrator"].predict_proba(
                y_prob[mask].reshape(-1, 1))[:, 1]
    return np.clip(out, 0.0, 1.0)
