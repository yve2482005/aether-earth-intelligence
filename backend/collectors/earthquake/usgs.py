"""USGS earthquake poller.

Feeds:
  https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_hour.geojson
  https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/significant_month.geojson
(USGS terms: data are public domain; polite automated use requested.)

One feed failing never crashes the other. Results flow through
normalizer -> deduplicator -> tracker.
"""
from __future__ import annotations

import sqlite3
from datetime import timedelta
from typing import Any, Dict, Optional

from backend.common.http import fetch_json, make_session
from backend.common.logging_setup import setup_logging
from backend.common.timeutil import now_utc, utc_iso
from backend.collectors.earthquake.event_normalizer import normalize_usgs_feature
from backend.collectors.earthquake.event_tracker import upsert

log = setup_logging("aether.collectors.earthquake.usgs")

FEEDS = {
    "all_hour": "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_hour.geojson",
    "significant_month": "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/significant_month.geojson",
}

# FDSN catch-up: M>=4.5 events from the past 24h. The summary feeds only
# cover the past hour (all_hour) plus significant events (significant_month,
# sig>=600). An M5.0+ event with sig<600 that ages out of all_hour during
# a collection gap is lost forever — this happened to us7000tw76 (Tanzania
# M5.1, sig 409) and us7000twg9 (Japan M6.1, reviewed product arrived
# after the hourly window). The catch-up query is cheap (dozens of events
# per day at M>=4.5) and upserts are idempotent, so re-fetching is safe.
FDSN_URL = "https://earthquake.usgs.gov/fdsnws/event/1/query"
CATCHUP_MIN_MAGNITUDE = 4.5
CATCHUP_WINDOW_HOURS = 24


def _record_health(conn: sqlite3.Connection, source_id: str, ok: bool,
                   latency_ms: Optional[float], records: int, message: Optional[str]) -> None:
    conn.execute(
        """INSERT INTO system_health (component, status, last_success, last_failure,
                                      latency_ms, records, message, checked_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (f"collector:{source_id}", "ok" if ok else "down",
         utc_iso() if ok else None, None if ok else utc_iso(),
         latency_ms, records, message, utc_iso()))
    conn.execute(
        """UPDATE source_registry SET last_success = CASE WHEN ? THEN ? ELSE last_success END,
                                      last_failure = CASE WHEN ? THEN last_failure ELSE ? END,
                                      health = ?
           WHERE source_id = ?""",
        (1 if ok else 0, utc_iso(), 1 if ok else 0, utc_iso(),
         "ok" if ok else "down", source_id))
    conn.commit()


def poll_feed(conn: sqlite3.Connection, feed_name: str, session) -> Dict[str, Any]:
    url = FEEDS[feed_name]
    source_id = f"usgs-earthquake-{feed_name.replace('_', '-')}"
    res = fetch_json(session, url)
    if not res["ok"]:
        _record_health(conn, source_id, False, res["latency_ms"], 0, res["error"])
        return {"ok": False, "feed": feed_name, "error": res["error"]}
    features = (res["data"] or {}).get("features") or []
    stats = {"inserted": 0, "updated": 0, "invalid": 0}
    for f in features:
        try:
            rec = normalize_usgs_feature(f)
            r = upsert(conn, rec)
            stats[r["action"]] = stats.get(r["action"], 0) + 1
        except Exception as e:  # one bad feature never kills the feed
            log.warning("feature skipped: %s", e)
            stats["invalid"] += 1
    total = stats["inserted"] + stats["updated"]
    _record_health(conn, source_id, True, res["latency_ms"], total, None)
    log.info("usgs %s: %s", feed_name, stats)
    return {"ok": True, "feed": feed_name, "features": len(features),
            "latency_ms": round(res["latency_ms"], 1), **stats}


def poll_catchup(conn: sqlite3.Connection, session) -> Dict[str, Any]:
    """FDSN catch-up: M>=4.5 from the past 24h (see FDSN_URL note above)."""
    session = session or make_session()
    end = now_utc()
    start = end - timedelta(hours=CATCHUP_WINDOW_HOURS)
    params = {
        "format": "geojson",
        "starttime": start.strftime("%Y-%m-%dT%H:%M:%S"),
        "endtime": end.strftime("%Y-%m-%dT%H:%M:%S"),
        "minmagnitude": CATCHUP_MIN_MAGNITUDE,
    }
    source_id = "usgs-earthquake-catchup"
    res = fetch_json(session, FDSN_URL, params=params)
    if not res["ok"]:
        _record_health(conn, source_id, False, res["latency_ms"], 0, res["error"])
        return {"ok": False, "feed": "catchup_m45_day", "error": res["error"]}
    features = (res["data"] or {}).get("features") or []
    stats = {"inserted": 0, "updated": 0, "invalid": 0}
    for f in features:
        try:
            rec = normalize_usgs_feature(f)
            r = upsert(conn, rec)
            stats[r["action"]] = stats.get(r["action"], 0) + 1
        except Exception as e:  # one bad feature never kills the feed
            log.warning("catchup feature skipped: %s", e)
            stats["invalid"] += 1
    total = stats["inserted"] + stats["updated"]
    _record_health(conn, source_id, True, res["latency_ms"], total, None)
    log.info("usgs catchup_m45_day: %s", stats)
    return {"ok": True, "feed": "catchup_m45_day", "features": len(features),
            "latency_ms": round(res["latency_ms"], 1), **stats}


def collect(conn: sqlite3.Connection, session=None) -> Dict[str, Any]:
    session = session or make_session()
    out: Dict[str, Any] = {"feeds": {}}
    for name in FEEDS:
        try:
            out["feeds"][name] = poll_feed(conn, name, session)
        except Exception as e:
            log.exception("feed %s failed", name)
            out["feeds"][name] = {"ok": False, "feed": name, "error": str(e)}
    try:
        out["feeds"]["catchup_m45_day"] = poll_catchup(conn, session)
    except Exception as e:
        log.exception("feed catchup_m45_day failed")
        out["feeds"]["catchup_m45_day"] = {
            "ok": False, "feed": "catchup_m45_day", "error": str(e)}
    return out
