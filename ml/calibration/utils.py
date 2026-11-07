"""Shared calibration utilities (used by ml/weather/calibrate.py).

All metrics here describe *data confidence* (how well predicted probabilities
match observed frequencies). They never express certainty about future events.
"""

from __future__ import annotations

import numpy as np


def brier_score(y_true: np.ndarray, y_prob: np.ndarray) -> float:
    y_true = np.asarray(y_true, dtype=float)
    y_prob = np.asarray(y_prob, dtype=float)
    return float(np.mean((y_prob - y_true) ** 2))


def reliability_table(y_true: np.ndarray, y_prob: np.ndarray,
                      n_bins: int = 10) -> list[dict]:
    """Per-bin mean predicted probability vs observed event frequency."""
    y_true = np.asarray(y_true, dtype=float)
    y_prob = np.asarray(y_prob, dtype=float)
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    table = []
    for b in range(n_bins):
        lo, hi = edges[b], edges[b + 1]
        if b == n_bins - 1:
            mask = (y_prob >= lo) & (y_prob <= hi)
        else:
            mask = (y_prob >= lo) & (y_prob < hi)
        cnt = int(mask.sum())
        table.append({
            "bin": b,
            "bin_low": round(float(lo), 3),
            "bin_high": round(float(hi), 3),
            "n": cnt,
            "mean_predicted": round(float(y_prob[mask].mean()), 4)
            if cnt else None,
            "observed_frequency": round(float(y_true[mask].mean()), 4)
            if cnt else None,
        })
    return table


def expected_calibration_error(y_true: np.ndarray, y_prob: np.ndarray,
                               n_bins: int = 10) -> float:
    """Weighted mean |observed_frequency - mean_predicted| over bins."""
    table = reliability_table(y_true, y_prob, n_bins)
    total = sum(r["n"] for r in table)
    if not total:
        return float("nan")
    ece = 0.0
    for r in table:
        if r["n"] and r["mean_predicted"] is not None:
            ece += (r["n"] / total) * abs(
                r["observed_frequency"] - r["mean_predicted"])
    return float(ece)


def crps_gaussian(y_true: np.ndarray, y_pred: np.ndarray,
                  sigma: float) -> float:
    """Empirical CRPS for a Gaussian predictive distribution.

    Assumption (documented): predictive distribution is Gaussian with mean =
    point forecast and a single sigma estimated from validation residuals.
    This is a pragmatic, stated approximation — not a claim about the true
    forecast distribution.
    """
    from math import erf, exp, pi, sqrt

    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    if not np.isfinite(sigma) or sigma <= 0:
        return float(np.mean(np.abs(y_true - y_pred)))  # = MAE fallback
    z = (y_true - y_pred) / sigma
    phi = np.exp(-0.5 * z ** 2) / sqrt(2 * pi)
    Phi = 0.5 * (1 + np.vectorize(erf)(z / sqrt(2)))
    crps = sigma * (z * (2 * Phi - 1) + 2 * phi - 1 / sqrt(pi))
    return float(np.mean(crps))
