"""GET /v1/engines/* and /v1/ocean/* — v2 data engines (spec §§22-30).

- /v1/engines/ensemble   — ensemble mean/median/quantiles/spread (MODEL OUTPUT)
- /v1/engines/weights    — evidence-based model weights (AETHER ANALYSIS)
- /v1/engines/cyclone    — honest cyclone status (no invented tracks)
- /v1/engines/flood      — hazard/exposure/risk kept separate (honest limits)
- /v1/engines/sunmoon    — sunrise/sunset from NWP daily output
- /v1/engines/aftershock — statistical aftershock probability, NOT a prediction
- /v1/engines/ocean      — real Open-Meteo marine data (FORECAST)

`router` serves /v1/engines/* (the coordinator's app loader includes `router`
only, so every endpoint lives on it).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.api._util import provenance, resolve_location, unavailable
from backend.db import db as dbmod
from backend.engines import (aftershock, cyclone, dynamic_weights, ensemble,
                             flood, ocean)

router = APIRouter(prefix="/v1/engines", tags=["engines"])


def _conn():
    c = dbmod.connect()
    try:
        yield c
    finally:
        c.close()


@router.get("/ensemble")
def get_ensemble(lat: Optional[float] = Query(None), lon: Optional[float] = Query(None),
                 location_id: Optional[int] = Query(None),
                 hours: int = Query(48, le=168),
                 conn=Depends(_conn)) -> Dict[str, Any]:
    loc = resolve_location(conn, lat, lon, location_id)
    payload = ensemble.ensemble_for_location(conn, loc["id"], hours=hours)
    prov = payload.pop("provenance", {})
    payload["location"] = {"id": loc["id"], "name": loc["name"]}
    payload["provenance"] = provenance(prov.get("sources", []), prov.get("updated_at"))
    return payload


@router.get("/weights")
def get_weights(lat: Optional[float] = Query(None), lon: Optional[float] = Query(None),
                location_id: Optional[int] = Query(None),
                variable: str = Query("temperature"),
                horizon_hours: int = Query(24, ge=0, le=168),
                conn=Depends(_conn)) -> Dict[str, Any]:
    loc = resolve_location(conn, lat, lon, location_id)
    return dynamic_weights.weights_for(conn, loc["id"], variable, horizon_hours)


@router.get("/cyclone")
def get_cyclone(conn=Depends(_conn)) -> Dict[str, Any]:
    return cyclone.status(conn)


@router.get("/flood")
def get_flood(lat: Optional[float] = Query(None), lon: Optional[float] = Query(None),
              location_id: Optional[int] = Query(None),
              hours: int = Query(72, le=168),
              conn=Depends(_conn)) -> Dict[str, Any]:
    loc = resolve_location(conn, lat, lon, location_id)
    return flood.assess(conn, loc["id"], hours=hours)


@router.get("/sunmoon")
def get_sunmoon(lat: Optional[float] = Query(None), lon: Optional[float] = Query(None),
                location_id: Optional[int] = Query(None),
                date: Optional[str] = Query(None),
                conn=Depends(_conn)) -> Dict[str, Any]:
    """Sunrise/sunset for a date (default today UTC) from NWP daily output."""
    from backend.common.timeutil import utc_iso
    loc = resolve_location(conn, lat, lon, location_id)
    day = date or utc_iso()[:10]
    row = dbmod.query_one(conn, """SELECT * FROM weather_daily
        WHERE location_id = ? AND date = ? ORDER BY collected_at DESC LIMIT 1""",
        (loc["id"], day))
    if row is None:
        check = dbmod.query_one(conn, """SELECT MAX(collected_at) AS t FROM weather_daily
            WHERE location_id = ?""", (loc["id"],))
        return unavailable(
            f"No sunrise/sunset data for {day} at this location yet.",
            ["open-meteo"], check["t"] if check else None, kind="FORECAST")
    return {"location_id": loc["id"], "date": day,
            "sunrise": row["sunrise"], "sunset": row["sunset"],
            "model": row["model"], "kind": "FORECAST",
            "note": ("Astronomical sunrise/sunset as reported in the NWP daily "
                     "product — deterministic, not a weather forecast."),
            "provenance": provenance([f"open-meteo/{row['model']}"], row["collected_at"])}


@router.get("/aftershock")
def get_aftershock(event_id: str = Query(...), conn=Depends(_conn)) -> Dict[str, Any]:
    r = dbmod.query_one(conn, """SELECT canonical_event_id, magnitude, origin_time
        FROM earthquake_events WHERE canonical_event_id = ?""", (event_id,))
    if r is None:
        raise HTTPException(404, f"earthquake event {event_id} not found")
    return aftershock.statistics_for(conn, r["magnitude"], r["origin_time"],
                                     mainshock_id=r["canonical_event_id"])


@router.get("/ocean")
def get_ocean_forecast(lat: Optional[float] = Query(None),
                       lon: Optional[float] = Query(None),
                       location_id: Optional[int] = Query(None),
                       hours: int = Query(48, le=168),
                       conn=Depends(_conn)) -> Dict[str, Any]:
    """Real Open-Meteo marine data (wave height/period/direction, sea temp)."""
    loc = resolve_location(conn, lat, lon, location_id)
    payload = ocean.latest(conn, loc["id"], hours=hours)
    if payload.get("status") == "unavailable":
        return payload
    prov = payload.pop("provenance", {})
    payload["location"] = {"id": loc["id"], "name": loc["name"]}
    payload["provenance"] = provenance(prov.get("sources", []), prov.get("updated_at"))
    return payload
