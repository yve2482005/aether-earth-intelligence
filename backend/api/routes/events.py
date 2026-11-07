"""GET /v1/events — unified earth_events feed with kind filters."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query

from backend.api._util import provenance
from backend.db import db as dbmod

router = APIRouter(prefix="/v1/events", tags=["events"])


def _conn():
    c = dbmod.connect()
    try:
        yield c
    finally:
        c.close()


@router.get("")
def list_events(kind: Optional[str] = Query(None),
                event_type: Optional[str] = Query(None),
                limit: int = Query(100, le=500),
                conn=Depends(_conn)) -> Dict[str, Any]:
    sql = "SELECT * FROM earth_events WHERE 1=1"
    params: list = []
    if kind:
        sql += " AND kind = ?"
        params.append(kind.upper())
    if event_type:
        sql += " AND event_type = ?"
        params.append(event_type.upper())
    sql += " ORDER BY start_time DESC LIMIT ?"
    params.append(limit)
    rows = dbmod.query(conn, sql, params)
    updated = max((r["updated_at"] for r in rows), default=None)
    return {"count": len(rows), "events": rows,
            "provenance": provenance(["aether-earth-events"], updated)}
