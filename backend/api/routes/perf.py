"""Perf routes (workstream F). Exposes `router = APIRouter()`.

Endpoints:
  GET  /v1/home/{location_id}?level=summary|details   one-response home bundle
  GET  /v1/map/events?bbox=&zoom=                     viewport-culled map features
  GET  /v1/perf/metrics?view=summary|details          real measured latencies
  GET  /v1/perf/schedule                             source cadence + due sources
  GET  /v1/perf/circuits                             circuit-breaker states
  GET  /v1/perf/notify/queue                         notification lane depths
  POST /v1/perf/notify/enqueue                       enqueue (never waits for delivery)
  POST /v1/perf/ml/precompute                        scheduled batch-predict job
  POST /v1/perf/migrate                              apply perf migration (idempotent)

WIRING NOTE: backend/api/app.py's _V2_ROUTE_MODULES tuple (owned by
workstream E) needs "perf" appended to mount this automatically. Until then
it is fully functional when mounted directly (tests do this).
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from backend.api._util import provenance
from backend.db import db as dbmod
from backend.perf import (geo, metrics as metrics_mod, ml_batch, notify_queue,
                          parallel, precompute, schedule)

router = APIRouter(prefix="/v1", tags=["perf"])

# process-wide parallel collector instance (shared circuit breakers)
_fast_collector = parallel.ParallelCollector(max_workers=6)


def _conn():
    c = dbmod.connect()
    try:
        yield c
    finally:
        c.close()


@router.get("/home/{location_id}")
def home_bundle(location_id: int,
                level: str = Query("summary", pattern="^(summary|details)$"),
                conn=Depends(_conn)) -> Dict[str, Any]:
    t0 = time.monotonic()
    trace = metrics_mod.new_trace()
    try:
        bundle = precompute.get_bundle(conn, location_id, level)
        cached = bundle is not None
        if bundle is None:
            bundle = precompute.build_home_bundle(conn, location_id, level)
            precompute.store_bundle(conn, location_id, level, bundle)
        bundle["confidence"]["parts_cached"] = cached
        latency_ms = (time.monotonic() - t0) * 1000.0
        metrics_mod.record("api", "home_bundle", latency_ms, trace_id=trace)
        bundle["perf"] = {"latency_ms": round(latency_ms, 2),
                          "served_from_cache": cached, "trace_id": trace}
        return bundle
    except KeyError:
        raise HTTPException(status_code=404, detail=f"location {location_id} not found")


_MAX_MAP_FEATURES = 2000  # hard cap: never send full global datasets (§72)


@router.get("/map/events")
def map_events(bbox: str = Query(..., description="min_lon,min_lat,max_lon,max_lat"),
               zoom: int = Query(5, ge=0, le=12),
               hours: int = Query(72, ge=1, le=720),
               conn=Depends(_conn)) -> Dict[str, Any]:
    """Viewport culling + zoom-dependent clustering. bbox is required; no
    full-world dumps. Hard cap of _MAX_MAP_FEATURES features."""
    t0 = time.monotonic()
    try:
        min_lon, min_lat, max_lon, max_lat = (float(x) for x in bbox.split(","))
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400,
                            detail="bbox must be 'min_lon,min_lat,max_lon,max_lat'")
    events = [dict(r) for r in conn.execute(
        """SELECT canonical_event_id AS event_id, magnitude, latitude, longitude,
                  depth, origin_time, region
           FROM earthquake_events
           WHERE origin_time > strftime('%Y-%m-%dT%H:%M:%SZ','now', ? || ' hours')
             AND latitude BETWEEN ? AND ? AND longitude BETWEEN ? AND ?""",
        (f"-{hours}", min_lat, max_lat, min_lon, max_lon))]
    culled = geo.viewport_cull(events, (min_lon, min_lat, max_lon, max_lat))
    if zoom <= 4:
        clusters = geo.cluster_by_cell(culled, res=3)
        features = [geo.cluster_summary(c, ev) for c, ev in clusters.items()]
        mode = "clustered_res3"
    elif zoom <= 7:
        clusters = geo.cluster_by_cell(culled, res=5)
        features = [geo.cluster_summary(c, ev) for c, ev in clusters.items()]
        mode = "clustered_res5"
    else:
        features = [{"type": "event", **e} for e in culled]
        mode = "individual"
    truncated = len(features) > _MAX_MAP_FEATURES
    features = features[:_MAX_MAP_FEATURES]
    latency_ms = (time.monotonic() - t0) * 1000.0
    metrics_mod.record("api", "map_events", latency_ms)
    return {"features": features, "count": len(features),
            "mode": mode, "truncated": truncated,
            "cap": _MAX_MAP_FEATURES, "events_considered": len(culled),
            "latency_ms": round(latency_ms, 2),
            "provenance": provenance(["aether-db"], None)}


@router.get("/perf/metrics")
def perf_metrics(view: str = Query("summary", pattern="^(summary|details)$")) -> Dict[str, Any]:
    data = metrics_mod.details() if view == "details" else metrics_mod.summary()
    return {**data, "honesty": "real measured latencies only; nothing estimated or fabricated"}


@router.get("/perf/schedule")
def perf_schedule(conn=Depends(_conn)) -> Dict[str, Any]:
    schedule.sync_defaults(conn)
    due = schedule.due_sources_db(conn)
    rows = [dict(r) for r in conn.execute(
        "SELECT source, interval_seconds, last_started_at, last_finished_at, "
        "last_status FROM perf_source_schedule ORDER BY source")]
    specs = {s: {"interval_seconds": c["interval_seconds"],
                 "justification": c["justification"]}
             for s, c in schedule.SOURCE_INTERVALS.items()}
    return {"sources": rows, "due_now": due, "spec": specs}


@router.get("/perf/circuits")
def perf_circuits() -> Dict[str, Any]:
    return {"breakers": _fast_collector.states(),
            "note": "shared process-wide ParallelCollector; opens after 3 "
                    "consecutive failures, half-open probe after 120s"}


@router.get("/perf/notify/queue")
def notify_queue_depth(conn=Depends(_conn)) -> Dict[str, Any]:
    return {"depth": notify_queue.depth(conn)}


class EnqueueIn(BaseModel):
    lane: str
    title: str
    body: str
    alert_id: Optional[str] = None
    payload: Optional[Dict[str, Any]] = None


@router.post("/perf/notify/enqueue")
def notify_enqueue(item: EnqueueIn, conn=Depends(_conn)) -> Dict[str, Any]:
    """Enqueue only — returns immediately, never waits for delivery (§29)."""
    qid = notify_queue.enqueue(conn, item.lane, item.title, item.body,
                               item.alert_id, item.payload)
    return {"queued_id": qid, "lane": item.lane,
            "note": "enqueued only; delivery happens in the background worker"}


@router.post("/perf/ml/precompute")
def ml_precompute(task: str = Query("temperature"), limit: int = Query(500, le=5000),
                  conn=Depends(_conn)) -> Dict[str, Any]:
    """Run the scheduled batch-predict job (never trains on the request path)."""
    return ml_batch.precompute_predictions(conn, task=task, limit=limit)


@router.post("/perf/migrate")
def perf_migrate() -> Dict[str, Any]:
    from backend.db import migrate_perf
    return {"applied": migrate_perf.apply(),
            "note": "idempotent; safe to run repeatedly"}
