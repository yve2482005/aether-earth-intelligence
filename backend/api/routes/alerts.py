"""GET /v1/alerts — active alerts with severity/priority filters."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query

from backend.api._util import provenance
from backend.db import db as dbmod

router = APIRouter(prefix="/v1/alerts", tags=["alerts"])


def _conn():
    c = dbmod.connect()
    try:
        yield c
    finally:
        c.close()


@router.get("")
def list_alerts(status: str = Query("active"),
                severity: Optional[str] = Query(None),
                type: Optional[str] = Query(None, alias="type"),
                limit: int = Query(100, le=500),
                conn=Depends(_conn)) -> Dict[str, Any]:
    sql = "SELECT * FROM alerts WHERE status = ?"
    params: list = [status]
    if severity:
        sql += " AND severity = ?"
        params.append(severity)
    if type:
        sql += " AND type = ?"
        params.append(type)
    sql += " ORDER BY priority ASC, issued_at DESC LIMIT ?"
    params.append(limit)
    rows = dbmod.query(conn, sql, params)
    items = [{**{k: r[k] for k in ("alert_id", "location_id", "event_id", "type", "severity",
                                   "priority", "title", "message", "source", "kind",
                                   "issued_at", "expires_at", "status")},
              "kind_detail": r["kind"]} for r in rows]
    updated = max((r["issued_at"] for r in rows), default=None)
    return {"count": len(items), "alerts": items,
            "provenance": provenance(["aether-alert-engine"], updated)}
