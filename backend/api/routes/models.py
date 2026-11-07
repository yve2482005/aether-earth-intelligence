"""GET /v1/models — registered model versions (read-only view of model_versions)."""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends

from backend.api._util import provenance
from backend.db import db as dbmod

router = APIRouter(prefix="/v1/models", tags=["models"])


def _conn():
    c = dbmod.connect()
    try:
        yield c
    finally:
        c.close()


@router.get("")
def models(conn=Depends(_conn)) -> Dict[str, Any]:
    rows = dbmod.query(conn, "SELECT * FROM model_versions ORDER BY deployed_at DESC NULLS LAST")
    if not rows:
        return {"status": "no_models", "count": 0, "models": [],
                "note": "No ML models registered yet (ML team owns model training).",
                "provenance": provenance(["aether-ml-registry"], None)}
    return {"count": len(rows), "models": rows,
            "provenance": provenance(["aether-ml-registry"],
                                     max(r["deployed_at"] for r in rows if r["deployed_at"]))}
