"""Workstream C notification/alert API (spec §§14, 16, 48, 99).

router = APIRouter() — the coordinator wires this into backend/api/app.py.
All endpoints are honest about FCM config state; nothing is reported as
delivered unless it was actually sent.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from backend.alerting import state_machine as sm
from backend.alerting.preferences import (clear_travel, get_prefs, get_travel,
                                          set_prefs, set_travel)
from backend.db import db as dbmod
from backend.notify import channels as chmod
from backend.notify import history as hist
from backend.notify import summary as summ
from backend.notify.fcm import (FCMError, FCMNotConfigured, dispatch_pending,
                                invalidate_token, is_configured,
                                refresh_token, register_token, send)

router = APIRouter(prefix="/v1/notify", tags=["notify"])


def _conn():
    c = dbmod.connect()
    try:
        yield c
    finally:
        c.close()


# ---- FCM config + channels ----
@router.get("/config")
def fcm_config() -> Dict[str, Any]:
    from backend.notify.fcm import is_configured as probe
    return {"fcm": probe(), "channels": chmod.as_list()}


@router.get("/channels")
def list_channels() -> Dict[str, Any]:
    return {"channels": chmod.as_list(),
            "android_payloads": chmod.android_channel_payloads()}


# ---- device tokens ----
class TokenIn(BaseModel):
    user_id: str = "local"
    token: str
    platform: str = "android"
    device_id: Optional[str] = None
    app_version: Optional[str] = None


@router.post("/tokens")
def post_token(body: TokenIn, conn=Depends(_conn)) -> Dict[str, Any]:
    if not body.token or len(body.token) < 8:
        raise HTTPException(400, "token looks invalid")
    return register_token(conn, body.user_id, body.token, body.platform,
                          body.device_id, body.app_version)


class TokenRefresh(BaseModel):
    device_id: str
    new_token: str


@router.post("/tokens/refresh")
def post_token_refresh(body: TokenRefresh, conn=Depends(_conn)) -> Dict[str, Any]:
    ok = refresh_token(conn, body.device_id, body.new_token)
    if not ok:
        raise HTTPException(404, "device not found")
    return {"device_id": body.device_id, "refreshed": True}


@router.delete("/tokens")
def delete_token(token: str = Query(...), conn=Depends(_conn)) -> Dict[str, Any]:
    n = invalidate_token(conn, token)
    return {"invalidated": n}


# ---- send / dispatch ----
@router.post("/send/{alert_id}")
def post_send(alert_id: str, user_id: Optional[str] = Query(None),
              dry_run: bool = Query(False), conn=Depends(_conn)) -> Dict[str, Any]:
    try:
        return send(conn, alert_id, user_id=user_id, dry_run=dry_run)
    except FCMNotConfigured as e:
        raise HTTPException(501, str(e))
    except FCMError as e:
        raise HTTPException(404, str(e))


@router.post("/dispatch")
def post_dispatch(user_id: Optional[str] = Query(None),
                  conn=Depends(_conn)) -> Dict[str, Any]:
    return dispatch_pending(conn, user_id=user_id)


# ---- history ----
@router.get("/history")
def get_history(recipient: Optional[str] = Query(None),
                unread_only: bool = Query(False),
                limit: int = Query(100, le=500),
                conn=Depends(_conn)) -> Dict[str, Any]:
    items = hist.list_history(conn, recipient, unread_only, limit)
    return {"count": len(items), "unread_count": hist.unread_count(conn, recipient),
            "items": items}


@router.post("/history/{notification_id}/read")
def post_read(notification_id: int, conn=Depends(_conn)) -> Dict[str, Any]:
    ok = hist.mark_read(conn, notification_id)
    if not ok:
        raise HTTPException(404, "notification not found")
    return {"id": notification_id, "read": True}


@router.post("/history/read-all")
def post_read_all(recipient: Optional[str] = Query(None),
                  conn=Depends(_conn)) -> Dict[str, Any]:
    return {"marked_read": hist.mark_all_read(conn, recipient)}


# ---- alert detail + revisions (state machine, spec §12) ----
@router.get("/alerts/{alert_id}")
def alert_detail(alert_id: str, conn=Depends(_conn)) -> Dict[str, Any]:
    sm.ensure_tables(conn)
    row = conn.execute("SELECT * FROM alerts WHERE alert_id = ?",
                       (alert_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "alert not found")
    return {"alert": dict(row), "revision_table": sm.revision_table_name(conn)}


@router.get("/alerts/{alert_id}/revisions")
def alert_revisions(alert_id: str, conn=Depends(_conn)) -> Dict[str, Any]:
    sm.ensure_tables(conn)
    exists = conn.execute("SELECT 1 FROM alerts WHERE alert_id = ?",
                          (alert_id,)).fetchone()
    if not exists:
        raise HTTPException(404, "alert not found")
    revs = sm.revisions(conn, alert_id)
    return {"alert_id": alert_id, "count": len(revs), "revisions": revs}


# ---- preferences: thresholds + silent hours (specs §16, §48) ----
@router.get("/preferences")
def get_preferences(user_id: str = Query("local"),
                    location_id: Optional[int] = Query(None),
                    conn=Depends(_conn)) -> Dict[str, Any]:
    prefs = get_prefs(conn, user_id, location_id)
    return {"user_id": user_id, "count": len(prefs), "preferences": prefs}


class PrefsIn(BaseModel):
    user_id: str = "local"
    location_id: Optional[int] = None
    label: Optional[str] = None
    radius_km: Optional[float] = None
    hazard_types: Optional[List[str]] = None
    min_severity: Optional[str] = None
    temp_high_c: Optional[float] = None
    temp_low_c: Optional[float] = None
    rain_mm_h: Optional[float] = None
    wind_kmh: Optional[float] = None
    aqi: Optional[float] = None
    quake_mag: Optional[float] = None
    silent_start: Optional[str] = None
    silent_end: Optional[str] = None
    silent_days: Optional[List[int]] = None
    silent_tz: Optional[str] = None
    silent_override_life_safety: Optional[int] = None
    enabled: Optional[int] = None


@router.put("/preferences")
def put_preferences(body: PrefsIn, conn=Depends(_conn)) -> Dict[str, Any]:
    import json
    fields = {k: v for k, v in body.dict().items()
              if k not in ("user_id",) and v is not None}
    if "hazard_types" in fields:
        fields["hazard_types"] = json.dumps(fields["hazard_types"])
    if "silent_days" in fields:
        fields["silent_days"] = json.dumps(fields["silent_days"])
    try:
        pref = set_prefs(conn, body.user_id, **fields)
    except Exception as e:
        raise HTTPException(400, f"invalid preferences: {e}")
    return {"user_id": body.user_id, "preference": pref}


# ---- travel mode (spec §99) ----
@router.get("/travel")
def get_travel_mode(user_id: str = Query("local"),
                    conn=Depends(_conn)) -> Dict[str, Any]:
    return {"user_id": user_id, "travel": get_travel(conn, user_id)}


class TravelIn(BaseModel):
    user_id: str = "local"
    destination_name: str
    latitude: float
    longitude: float
    start_date: str
    end_date: str


@router.put("/travel")
def put_travel_mode(body: TravelIn, conn=Depends(_conn)) -> Dict[str, Any]:
    if not (-90 <= body.latitude <= 90 and -180 <= body.longitude <= 180):
        raise HTTPException(400, "invalid coordinates")
    if body.start_date > body.end_date:
        raise HTTPException(400, "start_date must be <= end_date")
    return {"user_id": body.user_id,
            "travel": set_travel(conn, body.user_id, body.destination_name,
                                 body.latitude, body.longitude,
                                 body.start_date, body.end_date)}


@router.delete("/travel")
def delete_travel_mode(user_id: str = Query("local"),
                       conn=Depends(_conn)) -> Dict[str, Any]:
    clear_travel(conn, user_id)
    return {"user_id": user_id, "travel": None}


# ---- smart home summary (spec §98) ----
@router.get("/summary/daily")
def get_daily_summary(location_id: Optional[int] = Query(None),
                      user_id: str = Query("local"),
                      conn=Depends(_conn)) -> Dict[str, Any]:
    return summ.daily_summary(conn, location_id, user_id)
