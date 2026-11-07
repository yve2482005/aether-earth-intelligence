"""GDACS global disaster feed — Collector adapter (Engine 1).

Source: Global Disaster Alert and Coordination System (EU JRC / UN OCHA).
Free, no key. API returns GeoJSON; RSS is the fallback.
Docs: https://www.gdacs.org/gdacsapi/ (verified live 2026-11-06).

Event types: EQ earthquake, TC tropical cyclone, FL flood, VO volcano,
DR drought, WF wildfire. A GDACS entry is an official multi-agency alert,
NOT a model estimate — store it as-is, never infer a warning from it.

Honesty: GDACS severity is the provider's own score; keep it verbatim.
A missing GDACS event means "not in this feed", never "no disaster".
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from backend.common.http import fetch_json, make_session
from backend.common.logging_setup import setup_logging
from backend.common.timeutil import utc_iso

log = setup_logging("aether.collector.gdacs")

GDACS_API_URL = "https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH"
GDACS_RSS_URL = "https://www.gdacs.org/xml/rss.xml"
EVENT_TYPES = ("EQ", "TC", "FL", "VO", "DR", "WF")

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS gdacs_events (
    eventtype TEXT NOT NULL,
    eventid INTEGER NOT NULL,
    episodeid INTEGER,
    name TEXT,
    glide TEXT,
    severity TEXT,
    severity_value REAL,
    latitude REAL,
    longitude REAL,
    start_date TEXT,
    end_date TEXT,
    source_url TEXT,
    raw_json TEXT,
    collected_at TEXT NOT NULL,
    PRIMARY KEY (eventtype, eventid)
)
"""


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(CREATE_SQL)
    conn.commit()


def _parse_feature(feature: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    props = feature.get("properties") or {}
    geom = feature.get("geometry") or {}
    coords = geom.get("coordinates") or []
    eventtype = props.get("eventtype")
    eventid = props.get("eventid")
    if not eventtype or eventid is None:
        return None
    try:
        eventid = int(eventid)
    except (TypeError, ValueError):
        return None
    lon = coords[0] if len(coords) > 0 else None
    lat = coords[1] if len(coords) > 1 else None
    if lat is not None and not (-90.0 <= float(lat) <= 90.0):
        return None
    if lon is not None and not (-180.0 <= float(lon) <= 180.0):
        return None
    # The live API sometimes returns `url` as an object, not a string.
    url = props.get("url")
    if isinstance(url, dict):
        url = url.get("report") or url.get("details") or json.dumps(
            url, ensure_ascii=False)
    elif url is not None:
        url = str(url)
    return {
        "eventtype": str(eventtype),
        "eventid": eventid,
        "episodeid": props.get("episodeid"),
        "name": str(props.get("eventname") or props.get("name") or ""),
        "glide": str(props.get("glide") or ""),
        "severity": str(props.get("severity") or ""),
        "severity_value": props.get("severityvalue"),
        "latitude": lat,
        "longitude": lon,
        "start_date": props.get("fromdate"),
        "end_date": props.get("todate"),
        "source_url": url,
        "raw_json": json.dumps(feature, ensure_ascii=False)[:8000],
    }


def fetch_gdacs_events(session, days: int = 7) -> List[Dict[str, Any]]:
    """Fetch GDACS events for the last `days` days (all types).

    The SEARCH endpoint ignores the eventtype filter (verified live:
    six per-type calls each returned the same 82 events), so one call
    without the filter returns the full set.
    """
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days)
    params = {"from": start.isoformat(), "to": end.isoformat()}
    data = fetch_json(session, GDACS_API_URL, params=params)
    if not data.get("ok"):
        raise RuntimeError(data.get("error")
                           or "GDACS HTTP %s" % data.get("status_code"))
    features = ((data.get("data") or {}).get("features")) or []
    events: List[Dict[str, Any]] = []
    for feature in features:
        parsed = _parse_feature(feature)
        if parsed is not None:
            events.append(parsed)
    return events


def store_events(conn: sqlite3.Connection,
                 events: List[Dict[str, Any]]) -> Dict[str, int]:
    ensure_table(conn)
    now = utc_iso()
    inserted = 0
    for ev in events:
        cur = conn.execute(
            "INSERT OR IGNORE INTO gdacs_events "
            "(eventtype, eventid, episodeid, name, glide, severity,"
            " severity_value, latitude, longitude, start_date, end_date,"
            " source_url, raw_json, collected_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ev["eventtype"], ev["eventid"], ev.get("episodeid"),
             ev.get("name"), ev.get("glide"), ev.get("severity"),
             ev.get("severity_value"), ev.get("latitude"),
             ev.get("longitude"), ev.get("start_date"), ev.get("end_date"),
             ev.get("source_url"), ev.get("raw_json"), now))
        inserted += cur.rowcount
    conn.commit()
    return {"inserted": inserted, "seen": len(events),
            "skipped_duplicate": len(events) - inserted}


def collect(conn: sqlite3.Connection, session=None,
            days: int = 7) -> Dict[str, Any]:
    """Collect GDACS events into gdacs_events. Returns a run summary."""
    own_session = session is None
    session = session or make_session()
    try:
        events = fetch_gdacs_events(session, days=days)
    except Exception as exc:  # noqa: BLE001 - source failure, never empty result
        log.warning("gdacs fetch failed: %s", exc)
        return {"status": "source_unavailable", "fetched": 0,
                "inserted": 0, "error": str(exc)[:200]}
    try:
        stored = store_events(conn, events)
    except Exception as exc:  # noqa: BLE001 - persistence failure
        log.warning("gdacs store failed: %s", exc)
        return {"status": "persistence_error", "fetched": len(events),
                "inserted": 0, "error": str(exc)[:200]}
    return {"status": "success" if stored["inserted"] or stored["seen"] else "no_new_data",
            "fetched": stored["seen"], "inserted": stored["inserted"],
            "skipped_duplicate": stored["skipped_duplicate"]}
