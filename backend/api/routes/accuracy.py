"""GET /v1/accuracy — forecast verification scores (MAE/RMSE/Brier/CRPS/calibration).

Honest by construction: only verified model_scores rows are returned. With no
verification data yet, the endpoint says so explicitly.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.api._util import provenance
from backend.db import db as dbmod
from backend.trust.verification import (VerificationError, baseline_scores,
                                        forecast_vs_observed, model_comparison)

router = APIRouter(prefix="/v1/accuracy", tags=["accuracy"])


def _conn():
    c = dbmod.connect()
    try:
        yield c
    finally:
        c.close()


@router.get("")
def accuracy(location_id: Optional[int] = Query(None),
             variable: Optional[str] = Query(None),
             horizon: Optional[int] = Query(None),
             model: Optional[str] = Query(None),
             conn=Depends(_conn)) -> Dict[str, Any]:
    sql = "SELECT * FROM model_scores WHERE 1=1"
    params: list = []
    if location_id is not None:
        sql += " AND location_id = ?"
        params.append(location_id)
    if variable:
        sql += " AND variable = ?"
        params.append(variable)
    if horizon is not None:
        sql += " AND horizon = ?"
        params.append(horizon)
    if model:
        sql += " AND model = ?"
        params.append(model)
    sql += " ORDER BY period_end DESC LIMIT 200"
    rows = dbmod.query(conn, sql, params)
    if not rows:
        return {"status": "no_verification_data", "kind": "OBSERVATION",
                "message": ("No forecast verification scores computed yet. Scores are "
                            "published only after real forecast-vs-observation comparison."),
                "scores": [],
                "provenance": provenance(["aether-verification"], None)}
    return {"count": len(rows), "scores": rows, "kind": "OBSERVATION",
            "provenance": provenance(["aether-verification"],
                                     max(r["period_end"] for r in rows if r["period_end"]))}


@router.get("/baselines")
def baselines(location_id: int = Query(...),
              variable: str = Query("temperature"),
              horizon: int = Query(24),
              days: int = Query(90, le=365),
              conn=Depends(_conn)) -> Dict[str, Any]:
    """Persistence & climatology baselines (§17), computed from observations."""
    try:
        out = baseline_scores(conn, location_id, variable, horizon, days)
    except VerificationError as e:
        raise HTTPException(400, str(e))
    return {"kind": "OBSERVATION", **out,
            "provenance": provenance(["aether-verification"], out["period_end"])}


@router.get("/compare")
def compare(location_id: int = Query(...),
            variable: str = Query("temperature"),
            horizon: int = Query(24),
            model: str = Query("gfs"),
            window: int = Query(72),
            conn=Depends(_conn)) -> Dict[str, Any]:
    """Forecast-vs-actual series (§18) for 24h/48h/72h/7d windows."""
    try:
        out = forecast_vs_observed(conn, location_id, variable, horizon, model, window)
    except VerificationError as e:
        raise HTTPException(400, str(e))
    latest = out["series"][0]["valid_time"] if out["series"] else None
    return {"kind": "OBSERVATION", **out,
            "provenance": provenance(["aether-verification"], latest)}


@router.get("/models")
def models(location_id: int = Query(...),
           variable: str = Query("temperature"),
           horizon: int = Query(24),
           conn=Depends(_conn)) -> Dict[str, Any]:
    """Model comparison incl. AETHER baseline vs ML correction (§19).

    Historical skill only — AETHER never auto-declares a winner.
    """
    try:
        out = model_comparison(conn, location_id, variable, horizon)
    except VerificationError as e:
        raise HTTPException(400, str(e))
    latest = None
    for m in out["models"]:
        if m.get("period_end") and (latest is None or m["period_end"] > latest):
            latest = m["period_end"]
    return {"kind": "OBSERVATION", **out,
            "provenance": provenance(["aether-verification"], latest)}
