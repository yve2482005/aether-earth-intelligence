"""GET/POST /v1/locations."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from backend.api._util import provenance
from backend.common.timeutil import utc_iso
from backend.db import db as dbmod

router = APIRouter(prefix="/v1/locations", tags=["locations"])


def _conn():
    c = dbmod.connect()
    try:
        yield c
    finally:
        c.close()


class LocationIn(BaseModel):
    name: str
    latitude: float
    longitude: float
    timezone: Optional[str] = "UTC"
    country: Optional[str] = None
    is_coastal: int = 0


@router.get("")
def list_locations(conn=Depends(_conn)) -> Dict[str, Any]:
    rows = dbmod.query(conn, "SELECT * FROM locations ORDER BY id")
    return {"count": len(rows), "locations": rows,
            "provenance": provenance(["aether-locations"],
                                     max((r["created_at"] for r in rows), default=None))}


@router.post("")
def add_location(body: LocationIn, conn=Depends(_conn)) -> Dict[str, Any]:
    cur = conn.execute(
        """INSERT INTO locations (name, latitude, longitude, timezone, country, is_coastal, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (body.name, body.latitude, body.longitude, body.timezone, body.country,
         body.is_coastal, utc_iso()))
    conn.commit()
    row = dbmod.query_one(conn, "SELECT * FROM locations WHERE id = ?", (cur.lastrowid,))
    return {"location": row, "provenance": provenance(["aether-locations"], row["created_at"])}
