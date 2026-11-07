"""GET /v1/nowcast — 0–120 min observation-anchored nowcast (spec §24).

Windows 0–15 / 15–30 / 30–60 / 60–120 min. Each window is explicitly labeled
NOWCAST (observation-anchored) or NWP FORECAST. With no usable recent
observation the engine degrades honestly: status="nowcast_unavailable" and
only NWP forecast values are shown — never mislabeled as a nowcast.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Query

from backend.api._util import provenance, resolve_location
from backend.db import db as dbmod
from backend.engines import nowcast as nowcast_engine

router = APIRouter(prefix="/v1/nowcast", tags=["nowcast"])


def _conn():
    c = dbmod.connect()
    try:
        yield c
    finally:
        c.close()


@router.get("")
def get_nowcast(lat: Optional[float] = Query(None), lon: Optional[float] = Query(None),
                location_id: Optional[int] = Query(None),
                conn=Depends(_conn)) -> Dict[str, Any]:
    loc = resolve_location(conn, lat, lon, location_id)
    payload = nowcast_engine.build(conn, loc["id"], loc)
    prov = payload.pop("provenance", {})
    payload["provenance"] = provenance(prov.get("sources", []), prov.get("updated_at"))
    return payload
