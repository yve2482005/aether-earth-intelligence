"""GET/POST /v1/admin/* — internal operations dashboard (§94), model
monitoring (§95), incident system (§71), alert delivery audit (§79).

Internal tool: no auth system (per spec), not linked prominently in the PWA.
All numbers come from stored records; missing data is labeled missing.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from backend.api._util import provenance
from backend.api.routes.trust import map_health
from backend.common.config import DB_PATH
from backend.common.timeutil import age_minutes
from backend.db import db as dbmod
from backend.incidents.incidents import (
    add_timeline_note,
    delivery_stats,
    ensure_tables,
    get_incident,
    list_deliveries,
    list_incidents,
    record_delivery,
    record_failover,
    record_incident,
    recent_failovers,
    resolve_incident,
    set_status,
)
from backend.trust.quality import all_datasets_quality

router = APIRouter(prefix="/v1/admin", tags=["admin"])


def _conn():
    c = dbmod.connect()
    try:
        yield c
    finally:
        c.close()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe(conn, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    """Query that returns [] when the table does not exist (ops view must
    render what is there, never crash on a missing table)."""
    try:
        return dbmod.query(conn, sql, params)
    except Exception:  # noqa: BLE001 - missing table etc.
        return []


# ---------------- summary ----------------

TABLES_FOR_STORAGE = [
    "weather_forecasts", "weather_observations", "earthquake_events",
    "earthquake_updates", "tsunami_events", "tsunami_messages", "alerts",
    "model_scores", "model_versions", "training_runs", "system_health",
    "source_registry", "incidents", "notification_deliveries",
]


@router.get("/summary")
def summary(conn=Depends(_conn)) -> Dict[str, Any]:
    ensure_tables(conn)
    now = _now()

    # source health (canonical statuses)
    regs = _safe(conn, "SELECT * FROM source_registry ORDER BY source_id")
    sources = []
    for r in regs:
        m = map_health(r.get("health"), r.get("last_success"))
        sources.append({"source_id": r["source_id"], "provider": r["provider"],
                        "dataset": r["dataset"], "health": m["status"],
                        "health_reason": m["reason"],
                        "last_success": r.get("last_success"),
                        "data_age_min": (round(age_minutes(r["last_success"]), 1)
                                         if r.get("last_success") else None)})

    # data freshness per quality module
    freshness = [{"dataset": q["dataset"], "score": q["score"],
                  "label": q["label"], "age_minutes": q["age_minutes"],
                  "latest": q["latest"]}
                 for q in all_datasets_quality(conn)]

    # active hazards: strong recent quakes, active tsunami events, active alerts
    quakes = _safe(conn,
        """SELECT canonical_event_id, origin_time, magnitude, region, tsunami_flag
           FROM earthquake_events
           WHERE origin_time >= strftime('%Y-%m-%dT%H:%M:%SZ','now', '-7 days') AND magnitude >= 5.0
           ORDER BY magnitude DESC LIMIT 20""")
    tsunamis = _safe(conn,
        "SELECT tsunami_event_id, message_type, severity, status, issue_time, "
        "affected_region FROM tsunami_events WHERE status = 'active' LIMIT 20")
    alert_queue = _safe(conn,
        "SELECT alert_id, type, severity, priority, title, issued_at, expires_at "
        "FROM alerts WHERE status = 'active' ORDER BY priority, issued_at DESC LIMIT 50")

    # failed jobs from system_health
    failed_jobs = _safe(conn,
        "SELECT component, status, last_success, last_failure, message, checked_at "
        "FROM system_health WHERE status != 'ok' ORDER BY checked_at DESC LIMIT 50")

    # model monitoring (§95): champions, recent training runs, dataset size
    champions = _safe(conn,
        "SELECT version, task, dataset_version, deployed_at FROM model_versions "
        "WHERE is_champion = 1 ORDER BY deployed_at DESC")
    candidates = _safe(conn,
        "SELECT version, task, dataset_version, deployed_at FROM model_versions "
        "WHERE is_champion = 0 ORDER BY deployed_at DESC LIMIT 10")
    runs = _safe(conn,
        "SELECT run_id, task, dataset_version, started_at, ended_at, rows, status, "
        "deployed, notes FROM training_runs ORDER BY started_at DESC LIMIT 10")
    _ds = _safe(conn, "SELECT COUNT(*) AS n FROM weather_forecasts")
    dataset_size = _ds[0]["n"] if _ds else None

    # accuracy snapshot: latest scored rows per model
    accuracy = _safe(conn,
        "SELECT location_id, variable, horizon, model, mae, rmse, brier, crps, "
        "sample_count, period_end, model_version FROM model_scores "
        "ORDER BY period_end DESC LIMIT 10")

    # incidents: open + recent
    incidents_open = list_incidents(conn, status="open", limit=20)
    incidents_recent = list_incidents(conn, limit=10)

    # notification delivery stats
    deliveries = delivery_stats(conn)
    deliveries_recent = list_deliveries(conn, limit=20)

    # storage / API usage
    try:
        db_bytes = os.path.getsize(DB_PATH)
    except OSError:
        db_bytes = None
    table_counts = {}
    for t in TABLES_FOR_STORAGE:
        try:
            table_counts[t] = dbmod.table_count(conn, t)
        except Exception:  # noqa: BLE001 - table may not exist yet
            table_counts[t] = None

    return {
        "as_of": now,
        "sources": sources,
        "freshness": freshness,
        "failovers": recent_failovers(conn, 10),
        "active_hazards": {"quakes_7d_m5plus": quakes,
                           "tsunami_active": tsunamis},
        "alert_queue": {"count": len(alert_queue), "alerts": alert_queue},
        "failed_jobs": failed_jobs,
        "models": {"champions": champions, "candidates": candidates,
                   "training_runs": runs,
                   "forecast_dataset_rows": dataset_size},
        "accuracy_snapshot": accuracy,
        "incidents": {"open": incidents_open, "recent": incidents_recent},
        "notification_deliveries": {"stats": deliveries,
                                   "recent": deliveries_recent},
        "storage": {"db_bytes": db_bytes, "table_counts": table_counts},
        "note": "Internal operations view. All values are stored records; "
                "nothing here is estimated or simulated.",
        "provenance": provenance(["aether-admin"], now),
    }


# ---------------- incidents ----------------

class IncidentIn(BaseModel):
    type: str = Field(..., description="source_outage|model_failure|alert_failure|db_failure|other")
    component: str
    title: str
    detail: Optional[str] = None


class TimelineIn(BaseModel):
    event: str


class ResolveIn(BaseModel):
    resolution: str


class StatusIn(BaseModel):
    status: str


@router.get("/incidents")
def incidents(status: Optional[str] = Query(None), limit: int = Query(50, le=200),
              conn=Depends(_conn)) -> Dict[str, Any]:
    try:
        out = list_incidents(conn, status=status, limit=limit)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"incidents": out, "provenance": provenance(["aether-admin"], _now())}


@router.post("/incidents")
def create_incident(body: IncidentIn, conn=Depends(_conn)) -> Dict[str, Any]:
    try:
        inc = record_incident(conn, body.type, body.component, body.title, body.detail or "")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"incident": inc, "provenance": provenance(["aether-admin"], _now())}


@router.post("/incidents/{incident_id}/timeline")
def incident_timeline(incident_id: str, body: TimelineIn,
                      conn=Depends(_conn)) -> Dict[str, Any]:
    if not add_timeline_note(conn, incident_id, body.event):
        raise HTTPException(404, f"incident {incident_id} not found")
    return {"incident": get_incident(conn, incident_id),
            "provenance": provenance(["aether-admin"], _now())}


@router.post("/incidents/{incident_id}/resolve")
def incident_resolve(incident_id: str, body: ResolveIn,
                     conn=Depends(_conn)) -> Dict[str, Any]:
    if not resolve_incident(conn, incident_id, body.resolution):
        raise HTTPException(404, f"incident {incident_id} not found or already resolved")
    return {"incident": get_incident(conn, incident_id),
            "provenance": provenance(["aether-admin"], _now())}


@router.post("/incidents/{incident_id}/status")
def incident_status(incident_id: str, body: StatusIn,
                    conn=Depends(_conn)) -> Dict[str, Any]:
    try:
        inc = set_status(conn, incident_id, body.status)
    except KeyError:
        raise HTTPException(404, f"incident {incident_id} not found")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"incident": inc, "provenance": provenance(["aether-admin"], _now())}


# ---------------- alert delivery audit ----------------

class DeliveryIn(BaseModel):
    notification_id: int = Field(..., description="notification_history row id")
    channel: str
    status: str = Field(..., description="delivered|failed|unknown")
    provider_message_id: Optional[str] = None
    detail: Optional[str] = None


@router.get("/deliveries")
def deliveries(alert_id: Optional[str] = Query(None),
               limit: int = Query(100, le=500),
               conn=Depends(_conn)) -> Dict[str, Any]:
    return {"deliveries": list_deliveries(conn, alert_id=alert_id, limit=limit),
            "stats": delivery_stats(conn),
            "provenance": provenance(["aether-admin"], _now())}


@router.post("/deliveries")
def create_delivery(body: DeliveryIn, conn=Depends(_conn)) -> Dict[str, Any]:
    if body.status not in ("delivered", "failed", "unknown"):
        raise HTTPException(400, "status must be delivered|failed|unknown")
    try:
        did = record_delivery(conn, body.notification_id, body.channel, body.status,
                              body.provider_message_id, body.detail)
    except Exception as e:
        raise HTTPException(400, str(e))
    return {"delivery_id": did, "provenance": provenance(["aether-admin"], _now())}


# ---------------- source failover log ----------------

class FailoverIn(BaseModel):
    category: str
    from_source: str
    to_source: str
    reason: Optional[str] = None


@router.post("/failover")
def log_failover(body: FailoverIn, conn=Depends(_conn)) -> Dict[str, Any]:
    fid = record_failover(conn, body.category, body.from_source,
                          body.to_source, body.reason)
    return {"failover_id": fid, "provenance": provenance(["aether-admin"], _now())}
