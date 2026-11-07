"""Dynamic model weights (spec §23).

Evidence-based weights per (geography, variable, horizon): models earn weight
from measured skill in model_scores — never from a hard-coded ranking like
"ECMWF is best". Weight for a model is proportional to 1/RMSE (falling back to
1/MAE, then 1/Brier for probability variables), normalized to sum to 1.

With no (or too little) score evidence the engine returns equal weights and
says so explicitly — it never pretends evidence it does not have.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional

# Horizon buckets in forecast hours.
HORIZONS = {"short": (0, 24), "medium": (24, 72), "long": (72, 9999)}

MIN_SAMPLES = 20  # score rows below this do not count as evidence


def _horizon_bucket(h: int) -> str:
    for name, (lo, hi) in HORIZONS.items():
        if lo <= h < hi:
            return name
    return "long"


def _error_of(score: dict) -> Optional[float]:
    for key in ("rmse", "mae", "brier"):
        v = score.get(key)
        if v is not None and v > 0:
            return float(v)
    return None


def weights_for(conn: sqlite3.Connection, location_id: int, variable: str,
                horizon_hours: int,
                models: Optional[List[str]] = None) -> Dict[str, Any]:
    """Compute evidence-based weights for one (location, variable, horizon)."""
    models = models or ["gfs_seamless", "icon_seamless", "ecmwf_ifs", "jma_seamless"]
    bucket = _horizon_bucket(horizon_hours)
    lo, hi = HORIZONS[bucket]
    rows = conn.execute(
        """SELECT model, rmse, mae, brier, sample_count, model_version,
                  period_start, period_end
           FROM model_scores
           WHERE location_id = ? AND variable = ? AND horizon >= ? AND horizon < ?
             AND COALESCE(sample_count, 0) >= ?""",
        (location_id, variable, lo, hi, MIN_SAMPLES)).fetchall()
    scores = [dict(r) for r in rows]

    evidence = {s["model"]: s for s in scores if _error_of(s) is not None}
    if not evidence:
        w = round(1.0 / len(models), 4)
        return {
            "location_id": location_id, "variable": variable,
            "horizon_hours": horizon_hours, "horizon_bucket": bucket,
            "weights": {m: w for m in models},
            "evidence": "insufficient",
            "note": ("No score evidence meets the minimum sample threshold "
                     f"({MIN_SAMPLES}) for this geography/variable/horizon — "
                     "equal weights used. Weights are evidence-based, never hard-coded."),
            "kind": "AETHER ANALYSIS",
        }

    inv = {m: 1.0 / _error_of(s) for m, s in evidence.items()}
    # Models with evidence but missing from the requested list are ignored;
    # models without evidence share the residual equally? No — honest version:
    # only evidence-backed models get weight; others get 0 and are listed.
    total = sum(inv.values())
    weights = {m: (round(inv[m] / total, 4) if m in inv else 0.0) for m in models}
    basis = {m: {"error": _error_of(evidence[m]), "metric": next(
        k for k in ("rmse", "mae", "brier") if evidence[m].get(k)),
        "samples": evidence[m].get("sample_count"),
        "model_version": evidence[m].get("model_version"),
        "period": [evidence[m].get("period_start"), evidence[m].get("period_end")]}
        for m in models if m in evidence}
    return {
        "location_id": location_id, "variable": variable,
        "horizon_hours": horizon_hours, "horizon_bucket": bucket,
        "weights": weights,
        "weighted_models": sorted(inv),
        "unweighted_models": [m for m in models if m not in inv],
        "basis": basis,
        "evidence": "model_scores",
        "note": ("Weights proportional to 1/error from measured model_scores. "
                 "No hard-coded model ranking."),
        "kind": "AETHER ANALYSIS",
    }


def weighted_blend(values: Dict[str, Optional[float]],
                   weights: Dict[str, float]) -> Optional[float]:
    """Blend per-model values with weights. Returns None if nothing usable."""
    num = den = 0.0
    for m, v in values.items():
        w = weights.get(m, 0.0)
        if v is not None and w > 0:
            num += w * float(v)
            den += w
    return round(num / den, 2) if den > 0 else None
