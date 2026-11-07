"""GET /v1/earthquakes — detections from the USGS feed (kind=DETECTION)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.api._util import provenance
from backend.db import db as dbmod

router = APIRouter(prefix="/v1/earthquakes", tags=["earthquakes"])


def _conn():
    c = dbmod.connect()
    try:
        yield c
    finally:
        c.close()


def _prov(conn) -> Dict[str, Any]:
    row = dbmod.query_one(conn, """SELECT MAX(checked_at) AS t FROM system_health
        WHERE component LIKE 'collector:usgs-earthquake%'""")
    return provenance(["usgs"], row["t"] if row else None)


def _event_dict(r: dict) -> Dict[str, Any]:
    import json
    return {
        "canonical_event_id": r["canonical_event_id"],
        "kind": "DETECTION",
        "origin_time": r["origin_time"],
        "latitude": r["latitude"], "longitude": r["longitude"], "depth_km": r["depth"],
        "magnitude": r["magnitude"], "magnitude_type": r["magnitude_type"],
        "region": r["region"], "status": r["status"],
        "felt_count": r["felt_count"], "mmi": r["mmi"], "alert_level": r["alert_level"],
        "tsunami_flag": bool(r["tsunami_flag"]),
        "shakemap_available": bool(r["shakemap_available"]),
        "pager_available": bool(r["pager_available"]),
        "source_url": r["source_url"], "update_time": r["update_time"],
        "sources": ["usgs"],
    }


@router.get("")
def list_quakes(min_magnitude: float = Query(0.0), limit: int = Query(50, le=500),
                since: Optional[str] = Query(None), conn=Depends(_conn)) -> Dict[str, Any]:
    sql = "SELECT * FROM earthquake_events WHERE magnitude >= ?"
    params: list = [min_magnitude]
    if since:
        sql += " AND origin_time >= ?"
        params.append(since)
    sql += " ORDER BY origin_time DESC LIMIT ?"
    params.append(limit)
    rows = dbmod.query(conn, sql, params)
    return {"count": len(rows), "events": [_event_dict(r) for r in rows],
            "provenance": _prov(conn)}


@router.get("/{event_id}")
def get_quake(event_id: str, conn=Depends(_conn)) -> Dict[str, Any]:
    r = dbmod.query_one(conn, "SELECT * FROM earthquake_events WHERE canonical_event_id = ?",
                        (event_id,))
    if r is None:
        raise HTTPException(404, f"earthquake event {event_id} not found")
    updates = dbmod.query(conn, """SELECT * FROM earthquake_updates
        WHERE canonical_event_id = ? ORDER BY received_at""", (event_id,))
    return {**_event_dict(r), "revisions": updates, "provenance": _prov(conn)}
