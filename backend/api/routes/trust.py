"""GET /v1/trust/* — trust, lineage, data quality, source health.

Trust Index is a TRANSPARENCY INDICATOR, never a correctness claim.
Every response carries provenance. Health statuses use the canonical
set HEALTHY / DEGRADED / STALE / FAILED / DISABLED.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.api._util import provenance
from backend.common.timeutil import age_minutes, parse_iso
from backend.db import db as dbmod
from backend.incidents.incidents import recent_failovers
from backend.trust.lineage import build_lineage_chain
from backend.trust.quality import all_datasets_quality, dataset_quality
from backend.trust.trust_index import compute_trust_index

router = APIRouter(prefix="/v1/trust", tags=["trust"])


def _conn():
    c = dbmod.connect()
    try:
        yield c
    finally:
        c.close()


def map_health(health: Optional[str], last_success: Optional[str]) -> Dict[str, Any]:
    """Map registry health to the canonical HEALTHY/DEGRADED/STALE/FAILED/DISABLED set."""
    h = (health or "unknown").lower()
    age = age_minutes(last_success) if last_success else None
    if h == "down":
        return {"status": "FAILED", "reason": "last check reported down"}
    if h == "degraded":
        return {"status": "DEGRADED", "reason": "source reports degraded"}
    if h == "unavailable":
        return {"status": "DISABLED", "reason": "source not available / not configured"}
    if h == "ok":
        if age is not None and age > 24 * 60:
            return {"status": "STALE",
                    "reason": f"no successful retrieval for {round(age / 60, 1)}h"}
        return {"status": "HEALTHY", "reason": "recent successful retrieval"}
    # unknown
    if age is not None and age <= 24 * 60:
        return {"status": "HEALTHY",
                "reason": "health not checked, but retrieval succeeded recently"}
    return {"status": "STALE",
            "reason": "health unknown and no recent successful retrieval"}


@router.get("/lineage")
def lineage(kind: str = Query(...,
                              description="weather-forecast|weather-observation|quake|tsunami"),
            location_id: Optional[int] = Query(None),
            model: Optional[str] = Query(None),
            valid_time: Optional[str] = Query(None),
            observed_at: Optional[str] = Query(None),
            canonical_event_id: Optional[str] = Query(None),
            tsunami_event_id: Optional[str] = Query(None),
            conn=Depends(_conn)) -> Dict[str, Any]:
    kwargs: Dict[str, Any] = {}
    if kind == "weather-forecast":
        if not (location_id and model and valid_time):
            raise HTTPException(400, "need location_id, model, valid_time")
        kwargs = {"location_id": location_id, "model": model, "valid_time": valid_time}
    elif kind == "weather-observation":
        if not (location_id and observed_at):
            raise HTTPException(400, "need location_id, observed_at")
        kwargs = {"location_id": location_id, "observed_at": observed_at}
    elif kind == "quake":
        if not canonical_event_id:
            raise HTTPException(400, "need canonical_event_id")
        kwargs = {"canonical_event_id": canonical_event_id}
    elif kind == "tsunami":
        if not tsunami_event_id:
            raise HTTPException(400, "need tsunami_event_id")
        kwargs = {"tsunami_event_id": tsunami_event_id}
    else:
        raise HTTPException(400, f"unknown lineage kind: {kind}")
    try:
        chain = build_lineage_chain(conn, kind, **kwargs)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"kind": "OBSERVATION", **chain,
            "provenance": provenance(["aether-trust"],
                                     datetime.now(timezone.utc)
                                     .strftime("%Y-%m-%dT%H:%M:%SZ"))}


@router.get("/quality")
def quality(dataset: Optional[str] = Query(None),
            conn=Depends(_conn)) -> Dict[str, Any]:
    try:
        if dataset:
            out: List[Dict[str, Any]] = [dataset_quality(conn, dataset)]
        else:
            out = all_datasets_quality(conn)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"kind": "OBSERVATION", "datasets": out,
            "provenance": provenance(["aether-trust"],
                                     datetime.now(timezone.utc)
                                     .strftime("%Y-%m-%dT%H:%M:%SZ"))}


@router.get("/index")
def trust_index(location_id: Optional[int] = Query(None),
                conn=Depends(_conn)) -> Dict[str, Any]:
    try:
        idx = compute_trust_index(conn, location_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"kind": "RISK", **idx,
            "provenance": provenance(["aether-trust"], idx["computed_at"],
                                     {"data_age_min": idx["data_age_minutes"]})}


@router.get("/sources")
def sources(conn=Depends(_conn)) -> Dict[str, Any]:
    regs = dbmod.query(conn, "SELECT * FROM source_registry ORDER BY source_id")
    out = []
    for r in regs:
        mapped = map_health(r.get("health"), r.get("last_success"))
        out.append({
            "source_id": r["source_id"], "provider": r["provider"],
            "dataset": r["dataset"], "category": r["category"],
            "url": r.get("url"), "license": r.get("license"),
            "attribution": r.get("attribution"),
            "commercial_use": r.get("commercial_use"),
            "update_frequency": r.get("update_frequency"),
            "geographic_coverage": r.get("geographic_coverage"),
            "raw_health": r.get("health"),
            "health": mapped["status"], "health_reason": mapped["reason"],
            "last_success": r.get("last_success"),
            "last_failure": r.get("last_failure"),
            "data_age_min": (round(age_minutes(r["last_success"]), 1)
                             if r.get("last_success") else None),
            "reliability_score": r.get("reliability_score"),
        })
    failovers = recent_failovers(conn, 10)
    return {"kind": "OBSERVATION", "sources": out,
            "recent_failovers": failovers,
            "failover_note": ("When the active source for a category changes, "
                              "it is logged here and the UI must say "
                              "'Forecast source changed' with the data age."),
            "provenance": provenance(["aether-health"],
                                     datetime.now(timezone.utc)
                                     .strftime("%Y-%m-%dT%H:%M:%SZ"))}
