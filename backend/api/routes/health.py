"""GET /v1/health — component health from system_health + source_registry summary."""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends

from backend.api._util import provenance
from backend.db import db as dbmod

router = APIRouter(prefix="/v1/health", tags=["health"])


def _conn():
    c = dbmod.connect()
    try:
        yield c
    finally:
        c.close()


@router.get("")
def health(conn=Depends(_conn)) -> Dict[str, Any]:
    comps = dbmod.query(conn, """SELECT component, status, last_success, last_failure,
            latency_ms, records, message, MAX(checked_at) AS checked_at
        FROM system_health GROUP BY component ORDER BY component""")
    sources = dbmod.query(conn, """SELECT source_id, provider, dataset, category, health,
            last_success, last_failure FROM source_registry ORDER BY source_id""")
    updated = max([c["checked_at"] for c in comps if c["checked_at"]], default=None)
    ok = sum(1 for c in comps if c["status"] == "ok")
    return {"components": comps, "sources": sources,
            "summary": {"components": len(comps), "ok": ok},
            "provenance": provenance(["aether-health"], updated)}
