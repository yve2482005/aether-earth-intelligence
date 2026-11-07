"""Evaluation for weather post-processing models.

Metrics reported (per task, never blended into one score):
  * regression tasks: MAE, RMSE, empirical CRPS (Gaussian assumption,
    documented in ml.calibration.utils)
  * probability task (rain_probability): Brier score, calibration error (ECE),
    reliability-bin table

Slices: overall, per forecast-horizon band (0-24h, 24-72h, 72h+),
per location, per weather regime. Each slice with >=1 sample becomes a
model_scores row via :func:`write_scores`.

"Confidence" in every output means data confidence — agreement between past
predictions and observations — never certainty about future weather.
"""

from __future__ import annotations

import numpy as np

from ml.calibration.utils import (
    brier_score,
    crps_gaussian,
    expected_calibration_error,
    reliability_table,
)
from ml.weather.dataset import REGIMES, TASKS


def regression_metrics(y_true: np.ndarray, y_pred: np.ndarray,
                       sigma: float | None = None) -> dict:
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    mask = np.isfinite(y_true) & np.isfinite(y_pred)
    y_true, y_pred = y_true[mask], y_pred[mask]
    n = len(y_true)
    if n == 0:
        return {"n": 0, "mae": None, "rmse": None, "crps": None}
    err = y_true - y_pred
    mae = float(np.mean(np.abs(err)))
    rmse = float(np.sqrt(np.mean(err ** 2)))
    sig = float(np.std(err)) if sigma is None else sigma
    crps = crps_gaussian(y_true, y_pred, sig)
    return {"n": n, "mae": mae, "rmse": rmse, "crps": crps}


def probability_metrics(y_true: np.ndarray, y_prob: np.ndarray) -> dict:
    y_true = np.asarray(y_true, dtype=float)
    y_prob = np.asarray(y_prob, dtype=float)
    mask = np.isfinite(y_true) & np.isfinite(y_prob)
    y_true, y_prob = y_true[mask], y_prob[mask]
    n = len(y_true)
    if n == 0:
        return {"n": 0, "brier": None, "calibration_error": None,
                "reliability_bins": []}
    return {
        "n": n,
        "brier": brier_score(y_true, y_prob),
        "calibration_error": expected_calibration_error(y_true, y_prob),
        "reliability_bins": reliability_table(y_true, y_prob),
    }


def _horizon_band(h: float) -> str:
    if h <= 24:
        return "0-24h"
    if h <= 72:
        return "24-72h"
    return "72h+"


def _horizon_mid(band: str) -> int:
    return {"0-24h": 12, "24-72h": 48, "72h+": 96}[band]


def evaluate_task(task: str, y_true: np.ndarray, y_pred: np.ndarray,
                  forecast_hours: np.ndarray, location_ids: list[int],
                  regimes: list[str], period_start: str, period_end: str,
                  model_version: str) -> tuple[dict, list[dict]]:
    """Evaluate one task. Returns (summary_metrics, model_scores_rows)."""
    is_prob = task == "rain_prob"
    rows: list[dict] = []

    def one(yv, pv, loc, band):
        if is_prob:
            m = probability_metrics(yv, pv)
            return {
                "location_id": loc, "variable": task,
                "horizon": _horizon_mid(band), "model": model_version,
                "mae": None, "rmse": None,
                "brier": m["brier"], "crps": None,
                "calibration_error": m["calibration_error"],
                "sample_count": m["n"], "period_start": period_start,
                "period_end": period_end, "model_version": model_version,
            }
        m = regression_metrics(yv, pv)
        return {
            "location_id": loc, "variable": task,
            "horizon": _horizon_mid(band), "model": model_version,
            "mae": m["mae"], "rmse": m["rmse"], "brier": None,
            "crps": m["crps"], "calibration_error": None,
            "sample_count": m["n"], "period_start": period_start,
            "period_end": period_end, "model_version": model_version,
        }

    bands = [_horizon_band(h) for h in forecast_hours]
    # overall
    if is_prob:
        summary = probability_metrics(y_true, y_pred)
    else:
        summary = regression_metrics(y_true, y_pred)
    rows.append(one(y_true, y_pred, None, "0-24h"))
    rows[-1]["horizon"] = 0  # overall row uses horizon 0

    for band in ("0-24h", "24-72h", "72h+"):
        mask = np.array([b == band for b in bands])
        if mask.sum():
            rows.append(one(y_true[mask], y_pred[mask], None, band))
    for loc in sorted(set(location_ids)):
        mask = np.array([l == loc for l in location_ids])
        if mask.sum():
            rows.append(one(y_true[mask], y_pred[mask], loc, "0-24h"))
            rows[-1]["horizon"] = -1  # per-location aggregate marker
    for regime in REGIMES:
        mask = np.array([r == regime for r in regimes])
        if mask.sum():
            r = one(y_true[mask], y_pred[mask], None, "0-24h")
            r["horizon"] = -2  # per-regime aggregate marker
            r["model"] = f"{model_version}|regime={regime}"
            rows.append(r)
    return summary, rows


def write_scores(db_path: str, rows: list[dict]) -> int:
    """Persist evaluation slices to model_scores via the registry."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from backend.ml_support.registry import record_model_scores

    return record_model_scores(db_path, rows)
