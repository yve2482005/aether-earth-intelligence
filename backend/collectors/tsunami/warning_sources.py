"""Tsunami warning sources.

Two inputs:
1. USGS tsunami_flag on earthquake events (detected in usgs.py, stored in DB).
2. NWS tsunami alerts, fetched from the official NWS API (api.weather.gov);
   the legacy alerts.weather.gov CAP atom feed is kept as a fallback.

NOTE (2026-10-07): alerts.weather.gov/cap/us.php began failing with an empty
reply on ~2026-10-05 (persistent, not transient). api.weather.gov is the
official NWS API replacement and verified reachable; it is now the primary
source. Every message goes through message_parser (raw classification
preserved) and is stored in tsunami_events / tsunami_messages. Health is
recorded honestly.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional

from backend.common.http import fetch_json, make_session
from backend.common.logging_setup import setup_logging
from backend.common.timeutil import utc_iso
from backend.collectors.tsunami.message_parser import parse_atom_entry, parse_official_text

log = setup_logging("aether.collectors.tsunami.warning_sources")

NWS_ATOM_URL = "https://alerts.weather.gov/cap/us.php?x=0"  # legacy fallback
NWS_API_URL = "https://api.weather.gov/alerts/active"  # official NWS API (primary)
# PTWC Atom feed (2026-11-06 accuracy update): the NWS sources above are
# US-only and never carried the international PTWC product for the 2026-10-24
# Vanuatu M6.9 (verified: api.weather.gov returned 0 features for the whole
# threat window while the collector polled fine). PTWC publishes its own
# Atom feed; each entry links its bulletin text (.txt) via rel=alternate.
# Verified live 2026-11-06 with the project's requests session (tsunami.gov
# blocks bare curl, not this client): HTTP 200, valid Atom. The feed normally
# carries only the latest product(s), so the collector must poll every run.
PTWC_ATOM_URL = "https://www.tsunami.gov/events/xml/PHEBAtom.xml"
PTWC_ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}
# Event types the NWS uses for tsunami products; api.weather.gov accepts them as
# the `event` filter. Kept in official wording so message_parser sees the
# original classification verbatim.
NWS_TSUNAMI_EVENTS = (
    "Tsunami Warning",
    "Tsunami Advisory",
    "Tsunami Watch",
    "Tsunami Information Statement",
)


def _record_health(conn: sqlite3.Connection, component: str, source_id: Optional[str],
                   ok: bool, latency_ms: Optional[float], records: int,
                   message: Optional[str], status: str = "ok") -> None:
    conn.execute(
        """INSERT INTO system_health (component, status, last_success, last_failure,
                                      latency_ms, records, message, checked_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (component, status if ok else "down",
         utc_iso() if ok else None, None if ok else utc_iso(),
         latency_ms, records, message, utc_iso()))
    if source_id:
        conn.execute(
            """UPDATE source_registry SET last_success = CASE WHEN ? THEN ? ELSE last_success END,
                                          last_failure = CASE WHEN ? THEN last_failure ELSE ? END,
                                          health = ?
               WHERE source_id = ?""",
            (1 if ok else 0, utc_iso(), 1 if ok else 0, utc_iso(),
             "ok" if ok else "down", source_id))
    conn.commit()


def check_usgs_flags(conn: sqlite3.Connection, days: int = 7) -> List[Dict[str, Any]]:
    """Earthquake events with USGS tsunami_flag=1 in the last N days."""
    rows = conn.execute(
        """SELECT canonical_event_id, magnitude, region, origin_time, latitude, longitude
           FROM earthquake_events
           WHERE tsunami_flag = 1 AND origin_time >= strftime('%Y-%m-%dT%H:%M:%SZ','now',?)
           ORDER BY origin_time DESC""", (f"-{days} days",)).fetchall()
    return [dict(r) for r in rows]


def _api_feature_to_atom_entry(feature: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize one api.weather.gov GeoJSON alert feature into the atom-entry
    dict shape that parse_atom_entry consumes. Original wording preserved:
    title/headline stays verbatim, area names come from areaDesc."""
    p = feature.get("properties") or {}
    title = (p.get("headline") or p.get("event") or "").strip()
    parts = [p.get("description") or ""]
    if p.get("instruction"):
        parts.append("INSTRUCTION:\n" + p.get("instruction"))
    return {
        "title": title,
        "id": p.get("id") or "",
        "updated": p.get("sent") or p.get("effective") or "",
        "summary": "\n".join(x for x in parts if x).strip(),
        "link": p.get("id") or "",
        # CAP status verbatim ('Actual' / 'Test' / 'Exercise' / ...);
        # the parser uses it to keep test transmissions out of alerting.
        "cap_status": p.get("status") or "",
    }


def fetch_nws_tsunami_api(session=None) -> Dict[str, Any]:
    """Fetch tsunami alerts from the official NWS API (api.weather.gov).

    Queries each official tsunami event type; returns entries in the same
    shape as fetch_nws_tsunami so the parser/store path is unchanged.
    Verified live 2026-10-07: HTTP 200, valid GeoJSON, 0 active alerts.
    """
    import time
    session = session or make_session()
    t0 = time.monotonic()
    entries: List[Dict[str, Any]] = []
    seen_ids = set()
    try:
        for ev in NWS_TSUNAMI_EVENTS:
            res = fetch_json(session, NWS_API_URL, params={"event": ev})
            if not res.get("ok"):
                return {"ok": False, "error": f"{ev}: {res.get('error')}",
                        "status_code": res.get("status_code"),
                        "latency_ms": (time.monotonic() - t0) * 1000.0}
            for feature in (res.get("data") or {}).get("features", []):
                fid = (feature.get("properties") or {}).get("id") or ""
                if fid in seen_ids:
                    continue
                seen_ids.add(fid)
                entries.append(_api_feature_to_atom_entry(feature))
        return {"ok": True, "entries": entries,
                "latency_ms": (time.monotonic() - t0) * 1000.0,
                "source": "api.weather.gov"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "status_code": None, "latency_ms": (time.monotonic() - t0) * 1000.0}


def fetch_nws_tsunami(session=None) -> Dict[str, Any]:
    """Fetch the legacy NWS CAP atom feed (fallback only).

    Honesty: availability and terms are verified live at each check; the
    registry records the outcome. Nothing is fabricated when the feed is down.
    """
    import time
    import xml.etree.ElementTree as ET
    session = session or make_session()
    t0 = time.monotonic()
    try:
        resp = session.get(NWS_ATOM_URL, timeout=25)
        latency_ms = (time.monotonic() - t0) * 1000.0
        if resp.status_code != 200:
            return {"ok": False, "error": f"HTTP {resp.status_code}",
                    "status_code": resp.status_code, "latency_ms": latency_ms}
        root = ET.fromstring(resp.text)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "status_code": None, "latency_ms": (time.monotonic() - t0) * 1000.0}
    ns = {"a": "http://www.w3.org/2005/Atom"}
    entries = []
    for e in root.findall("a:entry", ns):
        title = (e.findtext("a:title", default="", namespaces=ns) or "")
        if "tsunami" not in title.lower():
            continue
        entries.append({
            "title": title,
            "id": e.findtext("a:id", default="", namespaces=ns),
            "updated": e.findtext("a:updated", default="", namespaces=ns),
            "summary": e.findtext("a:summary", default="", namespaces=ns),
            "link": (e.find("a:link", ns).get("href") if e.find("a:link", ns) is not None else ""),
        })
    return {"ok": True, "entries": entries, "latency_ms": latency_ms}


def _store_message(conn: sqlite3.Connection, parsed: Dict[str, Any]) -> str:
    tid = parsed["tsunami_event_id"]
    existing = conn.execute(
        "SELECT tsunami_event_id FROM tsunami_events WHERE tsunami_event_id = ?", (tid,)).fetchone()
    if existing is None:
        conn.execute(
            """INSERT INTO tsunami_events
               (tsunami_event_id, source, earthquake_event_id, message_type, severity,
                affected_region, issue_time, valid_time, arrival_time, status,
                source_url, raw_classification, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (tid, parsed["source"], parsed.get("earthquake_event_id"),
             parsed["message_type"], parsed.get("severity"), parsed.get("affected_region"),
             parsed.get("issue_time"), parsed.get("valid_time"), parsed.get("arrival_time"),
             parsed.get("status", "active"), parsed.get("source_url"),
             parsed.get("raw_classification"), utc_iso()))
    else:
        conn.execute(
            """UPDATE tsunami_events SET message_type=?, severity=?, affected_region=?,
               issue_time=?, valid_time=?, arrival_time=?, status=?, raw_classification=?,
               updated_at=? WHERE tsunami_event_id=?""",
            (parsed["message_type"], parsed.get("severity"), parsed.get("affected_region"),
             parsed.get("issue_time"), parsed.get("valid_time"), parsed.get("arrival_time"),
             parsed.get("status", "active"), parsed.get("raw_classification"), utc_iso(), tid))
    # v2: message versioning — sequence number within the event (v1, v2, ...).
    ver = conn.execute(
        "SELECT COUNT(*) AS c FROM tsunami_messages WHERE tsunami_event_id = ?",
        (tid,)).fetchone()["c"] + 1
    conn.execute(
        """INSERT INTO tsunami_messages
           (tsunami_event_id, message_type, raw_text, issue_time, received_at, source_url, version)
           VALUES (?,?,?,?,?,?,?)""",
        (tid, parsed["message_type"], parsed.get("raw_text"), parsed.get("issue_time"),
         utc_iso(), parsed.get("source_url"), ver))
    conn.commit()
    return tid


def fetch_ptwc_messages(session=None) -> Dict[str, Any]:
    """Fetch PTWC bulletin entries from the official PTWC Atom feed.

    Returns entries with the bulletin text URL (rel=alternate link) so the
    caller can fetch + parse each .txt. No live data is fabricated when
    the feed is down.
    """
    import xml.etree.ElementTree as ET
    session = session or make_session()
    try:
        resp = session.get(PTWC_ATOM_URL, timeout=25)
        if resp.status_code != 200:
            return {"ok": False, "error": f"HTTP {resp.status_code}",
                    "status_code": resp.status_code}
        root = ET.fromstring(resp.text)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "status_code": None}
    entries = []
    for e in root.findall("a:entry", PTWC_ATOM_NS):
        link = None
        for l in e.findall("a:link", PTWC_ATOM_NS):
            if l.get("rel") == "alternate":
                link = l.get("href")
        if not link:
            continue
        entries.append({
            "title": e.findtext("a:title", default="", namespaces=PTWC_ATOM_NS) or "",
            "id": e.findtext("a:id", default="", namespaces=PTWC_ATOM_NS) or "",
            "updated": e.findtext("a:updated", default="", namespaces=PTWC_ATOM_NS) or "",
            "link": link,
        })
    return {"ok": True, "entries": entries, "source": "ptwc-atom"}


def _store_ptwc_message(conn: sqlite3.Connection, parsed: Dict[str, Any]) -> str:
    """Store a PTWC bulletin unless its exact text is already stored for
    the event. The Atom feed re-lists the latest product on every poll;
    without this, _store_message would append a duplicate version each run.
    Returns 'stored' or 'skipped'."""
    existing = conn.execute(
        "SELECT 1 FROM tsunami_messages WHERE tsunami_event_id = ? AND raw_text = ? LIMIT 1",
        (parsed["tsunami_event_id"], parsed.get("raw_text"))).fetchone()
    if existing is not None:
        return "skipped"
    _store_message(conn, parsed)
    return "stored"


def collect(conn: sqlite3.Connection, session=None) -> Dict[str, Any]:
    session = session or make_session()
    out: Dict[str, Any] = {"usgs_flags": [], "nws": {}, "stored": 0}
    try:
        flags = check_usgs_flags(conn)
        out["usgs_flags"] = flags
        _record_health(conn, "collector:tsunami-usgs-flags", None, True, None,
                       len(flags), f"{len(flags)} flagged event(s) in last 7d")
    except Exception as e:
        out["usgs_flags_error"] = str(e)
    try:
        # Primary: official NWS API (api.weather.gov). Fallback: legacy atom feed.
        nws = fetch_nws_tsunami_api(session)
        if not nws.get("ok"):
            nws = fetch_nws_tsunami(session)
        out["nws"] = {k: v for k, v in nws.items() if k != "entries"}
        if nws.get("ok"):
            stored = 0
            for entry in nws["entries"]:
                parsed = parse_atom_entry(entry)
                _store_message(conn, parsed)
                stored += 1
            out["stored"] = stored
            _record_health(conn, "collector:tsunami-nws", "nws-tsunami-cap", True,
                           nws.get("latency_ms"), stored, None)
        else:
            # Honest unavailable: record, do not fabricate.
            _record_health(conn, "collector:tsunami-nws", "nws-tsunami-cap", False,
                           nws.get("latency_ms"), 0, nws.get("error"))
    except Exception as e:
        log.exception("nws tsunami fetch failed")
        out["nws"] = {"ok": False, "error": str(e)}
        _record_health(conn, "collector:tsunami-nws", "nws-tsunami-cap", False,
                       None, 0, str(e))
    try:
        # PTWC Atom feed (added 2026-11-06): international products the NWS
        # sources never carry. One bad bulletin never kills the rest.
        ptwc = fetch_ptwc_messages(session)
        out["ptwc"] = {k: v for k, v in ptwc.items() if k != "entries"}
        if ptwc.get("ok"):
            stored = skipped = 0
            for entry in ptwc["entries"]:
                try:
                    resp = session.get(entry["link"], timeout=25)
                    if resp.status_code != 200:
                        log.warning("ptwc bulletin fetch failed %s: HTTP %s",
                                    entry["link"], resp.status_code)
                        continue
                    parsed = parse_official_text(resp.text, source="ptwc",
                                                 source_url=entry["link"])
                    if _store_ptwc_message(conn, parsed) == "stored":
                        stored += 1
                    else:
                        skipped += 1
                except Exception as e:
                    log.warning("ptwc entry skipped %s: %s", entry.get("link"), e)
            out["ptwc_stored"] = stored
            out["ptwc_skipped"] = skipped
            _record_health(conn, "collector:tsunami-ptwc", "ptwc-atom", True,
                           None, stored, None)
        else:
            _record_health(conn, "collector:tsunami-ptwc", "ptwc-atom", False,
                           None, 0, ptwc.get("error"))
    except Exception as e:
        log.exception("ptwc fetch failed")
        out["ptwc"] = {"ok": False, "error": str(e)}
        _record_health(conn, "collector:tsunami-ptwc", "ptwc-atom", False,
                       None, 0, str(e))
    return out
