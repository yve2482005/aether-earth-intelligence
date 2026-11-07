"""GET /v1/replay — forecast replay / time machine (§§50-51).

Given a past date, returns what the forecast said then vs what was observed,
plus the quake sequence and tsunami message revisions in range.

Past and future are NEVER mixed: future dates are rejected (400), and the
response documents the separation rule it was built under.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.api._util import provenance
from backend.db import db as dbmod
from backend.replay.replay import ReplayError, replay

router = APIRouter(prefix="/v1/replay", tags=["replay"])


def _conn():
    c = dbmod.connect()
    try:
        yield c
    finally:
        c.close()


@router.get("")
def get_replay(date: str = Query(..., description="past date, YYYY-MM-DD"),
               location_id: Optional[int] = Query(None),
               conn=Depends(_conn)) -> Dict[str, Any]:
    try:
        rep = replay(conn, date, location_id)
    except ReplayError as e:
        raise HTTPException(400, str(e))
    return {"kind": "OBSERVATION", **rep,
            "provenance": provenance(["aether-replay"], rep["separation"]["as_of"])}
