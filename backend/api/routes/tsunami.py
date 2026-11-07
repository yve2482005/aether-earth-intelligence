"""GET /v1/tsunami — official tsunami messages only (kind=WARNING).

Raw classifications are preserved verbatim. Correlation with earthquakes says
"potentially relevant earthquake detected" — never "tsunami will occur".
When no official data is available, the response is honestly unavailable.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.api._util import provenance, unavailable
from backend.collectors.tsunami.arrival import arrival_for_event
from backend.collectors.tsunami.coastal_risk import compute_coastal_risk
from backend.collectors.tsunami.event_matcher import match_message_to_earthquakes
from backend.db import db as dbmod

router = APIRouter(prefix="/v1/tsunami", tags=["tsunami"])


def _conn():
    c = dbmod.connect()
    try:
        yield c
    finally:
        c.close()


def _msg_dict(r: dict) -> Dict[str, Any]:
    return {
        "tsunami_event_id": r["tsunami_event_id"],
        "kind": "WARNING",
        "message_type": r["message_type"],
        "raw_classification": r["raw_classification"],  # verbatim original
        "severity": r["severity"],
        "affected_region": r["affected_region"],
        "issue_time": r["issue_time"], "valid_time": r["valid_time"],
        "arrival_time": r["arrival_time"],
        "status": r["status"],
        "earthquake_event_id": r["earthquake_event_id"],
        "source": r["source"], "source_url": r["source_url"],
    }


@router.get("")
def list_tsunami(status: str = Query("active"), conn=Depends(_conn)) -> Dict[str, Any]:
    rows = dbmod.query(conn, "SELECT * FROM tsunami_events WHERE status = ? ORDER BY issue_time DESC",
                       (status,))
    check = dbmod.query_one(conn, """SELECT MAX(checked_at) AS t FROM system_health
        WHERE component LIKE 'collector:tsunami%'""")
    prov = provenance(["nws", "usgs"], check["t"] if check else None)
    if not rows and status == "active":
        return {"status": "no_active_messages", "kind": "WARNING",
                "message": ("No active official tsunami messages. "
                            "Official tsunami data currently unavailable beyond this check."),
                "events": [], "provenance": prov}
    return {"count": len(rows), "events": [_msg_dict(r) for r in rows], "provenance": prov}


@router.get("/{event_id}")
def get_tsunami(event_id: str, conn=Depends(_conn)) -> Dict[str, Any]:
    r = dbmod.query_one(conn, "SELECT * FROM tsunami_events WHERE tsunami_event_id = ?",
                        (event_id,))
    if r is None:
        raise HTTPException(404, f"tsunami event {event_id} not found")
    msgs = dbmod.query(conn, "SELECT * FROM tsunami_messages WHERE tsunami_event_id = ? ORDER BY id",
                       (event_id,))
    match = match_message_to_earthquakes(conn, event_id)
    arrival = arrival_for_event(conn, event_id)
    check = dbmod.query_one(conn, """SELECT MAX(checked_at) AS t FROM system_health
        WHERE component LIKE 'collector:tsunami%'""")
    return {**_msg_dict(r), "messages": msgs, "correlation": match, "arrival": arrival,
            "provenance": provenance(["nws", "usgs"], check["t"] if check else None)}


@router.get("/risk/coastal")
def coastal_risk(lat: float = Query(...), lon: float = Query(...),
                 place: str = Query(""), conn=Depends(_conn)) -> Dict[str, Any]:
    result = compute_coastal_risk(conn, lat, lon, place)
    result["provenance"] = provenance(["nws", "usgs"], result.get("assessed_at"))
    return result
