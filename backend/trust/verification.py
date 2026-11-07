"""Forecast verification engine (§§17-19).

Metrics (MAE/RMSE/bias/Brier/CRPS/calibration/reliability) evaluated BY
location x variable x horizon x model, using stored forecasts +
observations. Persistence & climatology baselines are computed from stored
observations, never assumed. Model comparison shows historical skill only —
it never auto-declares a winner.
"""
from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

VARIABLES = {
    "temperature", "feels_like", "humidity", "pressure", "wind_speed",
    "wind_direction", "precipitation", "precip_probability", "cloud_cover",
}

# windows (hours) the accuracy UI may request for forecast-vs-observed
WINDOWS = {24, 48, 72, 168}


class VerificationError(ValueError):
    """Raised when verification cannot be computed honestly (e.g. no pairs)."""


def compute_metrics(pairs: Sequence[Tuple[float, float]],
                    probabilities: Optional[Sequence[Tuple[float, float]]] = None
                    ) -> Dict[str, Any]:
    """Core metrics from (forecast, observed) pairs.

    probabilities: optional (forecast_prob_0_1, event_occurred_0_1) pairs for
    Brier + reliability. For deterministic forecasts CRPS equals MAE — the
    result says so explicitly rather than pretending at an ensemble.
    """
    vals = [(float(f), float(o)) for f, o in pairs
            if f is not None and o is not None
            and math.isfinite(float(f)) and math.isfinite(float(o))]
    n = len(vals)
    if n == 0:
        raise VerificationError("no matched forecast/observation pairs")
    errs = [f - o for f, o in vals]
    abs_errs = [abs(e) for e in errs]
    mae = sum(abs_errs) / n
    rmse = math.sqrt(sum(e * e for e in errs) / n)
    bias = sum(errs) / n
    out: Dict[str, Any] = {
        "sample_count": n,
        "mae": mae,
        "rmse": rmse,
        "bias": bias,
        "crps": mae,  # deterministic point forecast: CRPS == MAE
        "crps_note": "deterministic forecast — CRPS equals MAE by definition",
    }
    if probabilities is not None:
        probs = [(float(p), int(e)) for p, e in probabilities
                 if p is not None and e is not None]
        if probs:
            brier = sum((p - e) ** 2 for p, e in probs) / len(probs)
            out["brier"] = brier
            out["reliability"] = _reliability(probs)
            out["calibration_error"] = out["reliability"]["calibration_error"]
    return out


def _reliability(probs: Sequence[Tuple[float, float]], bins: int = 10) -> Dict[str, Any]:
    edges = [i / bins for i in range(bins + 1)]
    out_bins: List[Dict[str, Any]] = []
    weighted = 0.0
    total = 0
    for i in range(bins):
        lo, hi = edges[i], edges[i + 1]
        cell = [(p, e) for p, e in probs if (p >= lo and (p < hi or i == bins - 1))]
        if not cell:
            out_bins.append({"bin": [lo, hi], "forecast_mean": None,
                             "observed_freq": None, "n": 0})
            continue
        fm = sum(p for p, _ in cell) / len(cell)
        of = sum(e for _, e in cell) / len(cell)
        weighted += len(cell) * abs(fm - of)
        total += len(cell)
        out_bins.append({"bin": [round(lo, 2), round(hi, 2)],
                         "forecast_mean": round(fm, 3),
                         "observed_freq": round(of, 3), "n": len(cell)})
    return {"bins": out_bins,
            "calibration_error": round(weighted / total, 4) if total else None,
            "n": total}


def _check_variable(variable: str) -> None:
    if variable not in VARIABLES:
        raise VerificationError(
            f"unsupported variable '{variable}'; choose one of {sorted(VARIABLES)}")


def forecast_vs_observed(conn: sqlite3.Connection, location_id: int,
                         variable: str, horizon: int, model: str,
                         window_hours: int = 72,
                         limit: int = 5000) -> Dict[str, Any]:
    """Forecast-vs-actual pairs: forecast value vs what was observed.

    Joins weather_forecasts (forecast_hour = horizon) with weather_observations
    on valid_time = observed_at. Only pairs where an observation exists are
    used — missing observations are never filled in.
    """
    _check_variable(variable)
    if horizon < 0:
        raise VerificationError("horizon must be >= 0")
    if window_hours not in WINDOWS:
        raise VerificationError(f"window must be one of {sorted(WINDOWS)}")
    rows = conn.execute(
        f"""SELECT f.valid_time AS valid_time, f.{variable} AS forecast,
                   o.{variable} AS observed,
                   o.precipitation AS obs_precip, f.precip_probability AS f_prob
            FROM weather_forecasts f
            JOIN weather_observations o
              ON o.location_id = f.location_id
             AND o.observed_at = f.valid_time
            WHERE f.location_id = ? AND f.model = ? AND f.forecast_hour = ?
              AND f.{variable} IS NOT NULL AND o.{variable} IS NOT NULL
            ORDER BY f.valid_time DESC LIMIT ?""",
        (location_id, model, horizon, limit)).fetchall()
    pairs = [(r["forecast"], r["observed"]) for r in rows]
    if not pairs:
        raise VerificationError(
            "no matched forecast/observation pairs for this "
            "location x variable x horizon x model")
    probs = None
    prob_note = None
    if variable == "precip_probability":
        probs = []
        for r in rows:
            p = r["f_prob"]
            if p is None:
                continue
            p01 = p / 100.0 if p > 1.0 else p
            probs.append((p01, 1 if (r["obs_precip"] or 0) > 0.2 else 0))
        prob_note = ("Brier/reliability computed from precip_probability "
                     "(scaled to 0-1) vs observed event precipitation > 0.2 mm.")
    metrics = compute_metrics(pairs, probs)
    metrics["probability_note"] = prob_note
    series = [{"valid_time": r["valid_time"], "forecast": r["forecast"],
               "observed": r["observed"]} for r in rows]
    return {
        "location_id": location_id, "variable": variable, "horizon": horizon,
        "model": model, "window_hours": window_hours,
        "pairs_returned": len(series),
        "metrics": metrics,
        "series": series,  # newest first
        "method": ("matched stored forecasts to stored observations; "
                   "no imputation"),
    }


def baseline_scores(conn: sqlite3.Connection, location_id: int,
                    variable: str, horizon: int,
                    days: int = 90) -> Dict[str, Any]:
    """Persistence & climatology baselines computed from stored observations.

    persistence(h): observation at (t - h) used as the 'forecast' for t.
    climatology(h): mean of the same hour-of-day over the trailing `days`,
    excluding t itself.
    """
    _check_variable(variable)
    if variable == "precip_probability":
        raise VerificationError("baselines are defined for continuous variables only")
    if horizon < 0:
        raise VerificationError("horizon must be >= 0")
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    rows = conn.execute(
        f"""SELECT observed_at, {variable} AS v FROM weather_observations
            WHERE location_id = ? AND observed_at >= ? AND {variable} IS NOT NULL
            ORDER BY observed_at""",
        (location_id, start.strftime("%Y-%m-%dT%H:%M:%SZ"))).fetchall()
    obs = [(r["observed_at"], r["v"]) for r in rows]
    if len(obs) < 24:
        raise VerificationError(
            f"only {len(obs)} observations in trailing {days}d — "
            "not enough to build honest baselines")
    by_time = {t: v for t, v in obs}
    pers_pairs, clim_pairs = [], []
    hour_vals: Dict[int, List[float]] = {}
    for t, v in obs:
        hour_vals.setdefault(t[11:13], []).append(v)
    for t, v in obs:
        dt = datetime.strptime(t, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        past = dt - timedelta(hours=horizon)
        key = past.strftime("%Y-%m-%dT%H:%M:%SZ")
        if key in by_time:
            pers_pairs.append((by_time[key], v))
        pool = list(hour_vals.get(t[11:13], []))
        try:
            pool.remove(v)
        except ValueError:
            pass
        if pool:
            clim_pairs.append((sum(pool) / len(pool), v))
    out = {
        "location_id": location_id, "variable": variable, "horizon": horizon,
        "period_days": days,
        "period_start": obs[0][0], "period_end": obs[-1][0],
        "baselines": {},
        "method": ("computed from stored observations only; "
                   "persistence = value h hours earlier; "
                   "climatology = trailing mean of same hour-of-day"),
    }
    if pers_pairs:
        out["baselines"]["persistence"] = compute_metrics(pers_pairs)
        out["baselines"]["persistence"]["definition"] = (
            f"observation {horizon}h earlier used as forecast")
    if clim_pairs:
        out["baselines"]["climatology"] = compute_metrics(clim_pairs)
        out["baselines"]["climatology"]["definition"] = (
            "trailing same-hour-of-day mean (excluding target hour)")
    if not out["baselines"]:
        raise VerificationError("no baseline pairs could be formed")
    return out


def model_comparison(conn: sqlite3.Connection, location_id: int,
                     variable: str, horizon: int) -> Dict[str, Any]:
    """Side-by-side historical skill per model + baselines.

    Never declares a winner. Includes computed persistence/climatology
    baselines when the observations support them, so an 'AETHER baseline'
    row and any ML-corrected model row can be compared on equal footing.
    """
    _check_variable(variable)
    scored = conn.execute(
        """SELECT * FROM model_scores
           WHERE location_id = ? AND variable = ? AND horizon = ?
           ORDER BY period_end DESC""",
        (location_id, variable, horizon)).fetchall()
    scored = [dict(r) for r in scored]
    models = []
    for r in scored:
        models.append({
            "model": r["model"],
            "model_version": r.get("model_version"),
            "mae": r.get("mae"), "rmse": r.get("rmse"),
            "bias": r.get("bias"), "brier": r.get("brier"),
            "crps": r.get("crps"),
            "calibration_error": r.get("calibration_error"),
            "sample_count": r.get("sample_count"),
            "period_start": r.get("period_start"),
            "period_end": r.get("period_end"),
            "kind": "verified score (stored)",
        })
    try:
        base = baseline_scores(conn, location_id, variable, horizon)
        for name, m in base["baselines"].items():
            models.append({
                "model": name, "model_version": None,
                "mae": m["mae"], "rmse": m["rmse"], "bias": m["bias"],
                "brier": m.get("brier"), "crps": m.get("crps"),
                "calibration_error": m.get("calibration_error"),
                "sample_count": m["sample_count"],
                "period_start": base["period_start"],
                "period_end": base["period_end"],
                "kind": "computed baseline (from observations)",
            })
    except VerificationError as e:
        base = {"error": str(e)}
    return {
        "location_id": location_id, "variable": variable, "horizon": horizon,
        "models": models,
        "note": ("Historical skill only. AETHER never auto-declares a model "
                 "winner; compare sample counts and periods before judging."),
    }
