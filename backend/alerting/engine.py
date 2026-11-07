"""Alert engine: build alerts from earth_events + official warnings.

Priority (per CONTRACTS): 1 official life-safety ... 5 low-confidence analysis.
  1 = official life-safety warning (NWS tsunami warning, official alert)
  2 = verified observation of a significant hazard (M>=6.5 quake, PAGER red/orange)
  3 = official advisory/watch or high-confidence system analysis
  4 = medium-confidence system analysis
  5 = low-confidence analysis

kind: official | verified_observation | system_analysis
Alerts carry severity filters, dedup_key (INSERT ... ON CONFLICT DO NOTHING),
and expiry timestamps. Expired alerts are marked, never deleted.

v2: every alert is driven through the alert state machine
(DETECTED -> VALIDATING -> CONFIRMED -> SEVERITY_ASSESSED -> ALERTED/UPDATED ->
RESOLVED -> ARCHIVED). Revisions of the same event (M6.2 -> M6.4 -> M6.5)
merge into ONE alert row with revisions; notification is re-armed only on a
meaningful severity escalation. Personalized thresholds (alert_thresholds)
and travel-mode destinations are evaluated against real collected data.
"""
from __future__ import annotations

import math
import sqlite3
from datetime import timedelta
from typing import Any, Dict, List, Optional

from backend.alerting import state_machine as sm
from backend.alerting.dedup import make_dedup_key
from backend.alerting.preferences import (ensure_tables as prefs_ensure,
                                          get_prefs, get_travel,
                                          travel_active)
from backend.alerting.severity import (is_meaningful_escalation,
                                       magnitude_to_severity, official_to_severity,
                                       pager_to_severity, severity_rank,
                                       to_new_severity)
from backend.common.logging_setup import setup_logging
from backend.common.timeutil import now_utc, parse_iso, utc_iso

log = setup_logging("aether.alerting.engine")

INSERT_SQL = """INSERT INTO alerts
(alert_id, location_id, event_id, type, severity, priority, title, message,
 source, kind, issued_at, expires_at, status, dedup_key,
 version, state, confidence, notification_status)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,'DETECTED',?, 'pending')
ON CONFLICT(dedup_key) DO NOTHING"""

TERMINAL_STATES = ("RESOLVED", "ARCHIVED")


def _expire_old(conn: sqlite3.Connection) -> int:
    rows = conn.execute(
        """SELECT alert_id FROM alerts
           WHERE status = 'active' AND expires_at IS NOT NULL AND expires_at < ?""",
        (utc_iso(),)).fetchall()
    for r in rows:
        conn.execute("UPDATE alerts SET status = 'expired' WHERE alert_id = ?",
                     (r["alert_id"],))
        try:
            sm.expire(conn, r["alert_id"], reason="expired")
        except sm.InvalidTransition:
            pass
    conn.commit()
    return len(rows)


def _existing_event_alert(conn: sqlite3.Connection, event_id: Optional[str],
                          type_: str, alert_id: Optional[str] = None
                          ) -> Optional[Dict[str, Any]]:
    """Active, non-terminal alert for the same event (spec §13 merge).

    Matches on event_id + type first; falls back to alert_id (used for
    official tsunami messages whose event_id may be None)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(alerts)").fetchall()}
    if "state" not in cols:
        return None
    row = None
    if event_id:
        row = conn.execute(
            """SELECT * FROM alerts
               WHERE event_id = ? AND type = ? AND status = 'active'
                 AND state NOT IN ('RESOLVED','ARCHIVED')
               ORDER BY version DESC LIMIT 1""", (event_id, type_)).fetchone()
    if row is None and alert_id:
        row = conn.execute(
            """SELECT * FROM alerts
               WHERE alert_id = ? AND status = 'active'
                 AND state NOT IN ('RESOLVED','ARCHIVED')
               LIMIT 1""", (alert_id,)).fetchone()
    return dict(row) if row else None


def _apply_revision(conn: sqlite3.Connection, existing: Dict[str, Any],
                    alert: Dict[str, Any]) -> bool:
    """Merge an updated signal into the existing alert: bump version, record
    an UPDATED transition, and re-arm notification only on meaningful
    severity escalation. Returns True when merged."""
    old_sev = existing.get("severity")
    new_sev = alert["severity"]
    # Idempotent re-run: nothing changed -> no revision, no re-notification.
    if (old_sev == new_sev and existing.get("title") == alert.get("title")
            and existing.get("message") == alert.get("message")):
        return False
    escalated = is_meaningful_escalation(old_sev, new_sev)
    reason = (f"revision: {old_sev} -> {new_sev}"
              + (" (severity escalation)" if escalated else ""))
    new_version = (existing.get("version") or 1) + 1
    conn.execute(
        """UPDATE alerts SET severity = ?, priority = ?, title = ?, message = ?,
                          expires_at = ?, version = ?,
                          notification_status = ?
           WHERE alert_id = ?""",
        (new_sev, alert["priority"], alert["title"], alert["message"],
         alert.get("expires_at"), new_version,
         "escalation_pending" if escalated else "pending",
         existing["alert_id"]))
    conn.commit()
    try:
        sm.transition(conn, existing["alert_id"], "UPDATED", reason,
                      actor="alert-engine", bump_version=False)
    except sm.InvalidTransition:
        log.warning("could not transition %s to UPDATED", existing["alert_id"])
    return True


def _issue(conn: sqlite3.Connection, alert: Dict[str, Any]) -> bool:
    sm.ensure_tables(conn)  # idempotent: guarantees version/state columns exist
    # Merge-first: same event + type -> revision, not a new alert.
    existing = _existing_event_alert(conn, alert.get("event_id"), alert["type"],
                                     alert.get("alert_id"))
    if existing:
        _apply_revision(conn, existing, alert)
        return False
    # An alert row with this alert_id may already exist in a non-mergeable
    # state (expired, or state RESOLVED/ARCHIVED): the merge lookup above only
    # matches active alerts, and INSERT_SQL's ON CONFLICT covers dedup_key,
    # not the alert_id primary key — inserting would raise IntegrityError and
    # abort the caller's whole alert loop. Never resurrect a closed alert.
    if alert.get("alert_id"):
        prior = conn.execute(
            "SELECT status, state FROM alerts WHERE alert_id = ?",
            (alert["alert_id"],)).fetchone()
        if prior is not None:
            log.info("skipping %s: prior alert exists (status=%s, state=%s)",
                     alert["alert_id"], prior[0], prior[1])
            return False
    try:
        cur = conn.execute(INSERT_SQL, (
            alert["alert_id"], alert.get("location_id"), alert.get("event_id"),
            alert["type"], alert["severity"], alert["priority"], alert["title"],
            alert["message"], alert["source"], alert["kind"], alert["issued_at"],
            alert.get("expires_at"), "active", alert["dedup_key"],
            alert.get("confidence", "medium")))
    except sqlite3.IntegrityError as e:
        # Belt-and-braces: one conflicting row must never abort the alert loop.
        log.warning("insert skipped for %s: %s", alert.get("alert_id"), e)
        return False
    conn.commit()
    inserted = cur.rowcount > 0
    if inserted:
        try:
            sm.bootstrap(conn, alert["alert_id"])
        except sm.InvalidTransition as e:
            log.warning("state bootstrap failed for %s: %s", alert["alert_id"], e)
    return inserted


def alerts_from_tsunami(conn: sqlite3.Connection) -> int:
    """Priority 1 for official warnings/advisories/watches; kind=official."""
    from backend.collectors.tsunami.message_parser import is_test_message
    rows = conn.execute(
        "SELECT * FROM tsunami_events WHERE status = 'active'").fetchall()
    n = 0
    for r in rows:
        r = dict(r)
        # Belt-and-braces: rows stored before the parser's test-message
        # fix can still carry status='active' for an NWS test
        # transmission (e.g. nws-68f99fa5d1efaa23). Never alert on them.
        if is_test_message(r.get("raw_classification") or ""):
            continue
        sev = official_to_severity(r.get("message_type"))
        priority = 1 if sev in ("critical", "warning") else 3
        issued = r.get("issue_time") or utc_iso()
        expires_dt = parse_iso(issued)
        expires = utc_iso(expires_dt + timedelta(hours=24)) if expires_dt else None
        alert = {
            "alert_id": f"tsunami-{r['tsunami_event_id']}",
            "event_id": r.get("earthquake_event_id"),
            "type": "TSUNAMI", "severity": sev, "priority": priority,
            "title": f"Official tsunami message: {r.get('message_type')}",
            "message": (f"Original classification preserved: '{r.get('raw_classification')}'. "
                        f"Source: {r.get('source')}. This is an official message, not an AETHER prediction."),
            "source": r.get("source") or "official", "kind": "official",
            "issued_at": issued, "expires_at": expires,
            "confidence": "high",
            "dedup_key": make_dedup_key(r.get("source") or "official", "TSUNAMI", sev,
                                        event_id=r["tsunami_event_id"]),
        }
        if _issue(conn, alert):
            n += 1
    return n


def alerts_from_earthquakes(conn: sqlite3.Connection, min_mag: float = 6.0) -> int:
    """Priority 2 verified_observation alerts for significant quakes (last 48h)."""
    rows = conn.execute(
        """SELECT * FROM earthquake_events
           WHERE magnitude >= ? AND origin_time >= strftime('%Y-%m-%dT%H:%M:%SZ','now', '-2 days')
           ORDER BY magnitude DESC""", (min_mag,)).fetchall()
    n = 0
    for r in rows:
        r = dict(r)
        sev = magnitude_to_severity(r.get("magnitude"))
        if r.get("alert_level"):
            sev = pager_to_severity(r["alert_level"])
        if sev == "info" and (r.get("magnitude") or 0) < 6.5:
            continue
        issued = r.get("origin_time") or utc_iso()
        expires_dt = parse_iso(issued)
        expires = utc_iso(expires_dt + timedelta(hours=48)) if expires_dt else None
        alert = {
            "alert_id": f"quake-{r['canonical_event_id']}",
            "event_id": r["canonical_event_id"],
            "type": "EARTHQUAKE", "severity": sev, "priority": 2,
            "title": f"M{r.get('magnitude')} earthquake — {r.get('region')}",
            "message": (f"Detected M{r.get('magnitude')} ({r.get('magnitude_type')}) at "
                        f"{r.get('origin_time')}, depth {r.get('depth')} km, {r.get('region')}. "
                        f"Source: USGS. This is a detection of a past event, not a prediction."),
            "source": "usgs", "kind": "verified_observation",
            "issued_at": issued, "expires_at": expires,
            "confidence": "high",
            "dedup_key": make_dedup_key("usgs", "EARTHQUAKE", sev,
                                        event_id=r["canonical_event_id"]),
        }
        if _issue(conn, alert):
            n += 1
    return n


# ---------------------------------------------------------------------------
# Personalized thresholds (spec §48) + travel mode (§99)
# ---------------------------------------------------------------------------

def _json_loads(s):
    import json as _j
    return _j.loads(s) if isinstance(s, str) else s


def _haversine_km(lat1, lon1, lat2, lon2) -> float:
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _nearest_location_id(conn: sqlite3.Connection, lat: float, lon: float) -> Optional[int]:
    best, best_d = None, None
    for r in conn.execute("SELECT id, latitude, longitude FROM locations").fetchall():
        d = _haversine_km(lat, lon, r["latitude"], r["longitude"])
        if best_d is None or d < best_d:
            best, best_d = r["id"], d
    return best


def _passes_gate(pref: Dict[str, Any], type_: str, severity: str) -> bool:
    hazards = pref.get("hazard_types")
    if hazards:
        try:
            allowed = [h.upper() for h in (_json_loads(hazards) or [])]
            if allowed and type_.upper() not in allowed:
                return False
        except Exception:
            pass
    min_sev = (pref.get("min_severity") or "INFORMATION").upper()
    return severity_rank(severity) >= severity_rank(min_sev)


def _latest_run_stats(conn: sqlite3.Connection, location_id: int) -> Optional[Dict[str, Any]]:
    """Max forecast values over the next 24h from the latest model run (real data)."""
    row = conn.execute(
        """SELECT forecast_run FROM weather_forecasts
           WHERE location_id = ? ORDER BY forecast_run DESC LIMIT 1""",
        (location_id,)).fetchone()
    if not row:
        return None
    run = row["forecast_run"]
    stats = conn.execute(
        """SELECT MAX(temperature) AS tmax, MIN(temperature) AS tmin,
                  MAX(precipitation) AS pmax, MAX(wind_speed) AS wmax,
                  MAX(aqi) AS aqimax, GROUP_CONCAT(DISTINCT model) AS models
           FROM weather_forecasts
           WHERE location_id = ? AND forecast_run = ?
             AND forecast_hour BETWEEN 0 AND 24""",
        (location_id, run)).fetchone()
    return dict(stats) if stats else None


def _threshold_alerts_for(conn: sqlite3.Connection, location_id: int,
                           pref: Dict[str, Any], label_prefix: str = "") -> int:
    """Evaluate one preference row against real data. One alert per
    location+variable+day (dedup window = UTC date)."""
    n = 0
    day = now_utc().strftime("%Y-%m-%d")
    loc_row = conn.execute("SELECT name FROM locations WHERE id = ?",
                           (location_id,)).fetchone()
    loc_name = loc_row["name"] if loc_row else f"location {location_id}"

    def maybe_issue(type_: str, variable: str, value: float, threshold: float,
                    unit: str, severity: str = "advisory"):
        nonlocal n
        if not _passes_gate(pref, type_, severity):
            return
        dedup_key = make_dedup_key("aether-threshold", type_, severity,
                                   event_id=None, location_id=location_id,
                                   window=f"{variable}:{day}")
        alert = {
            "alert_id": f"thr-{location_id}-{variable}-{day}",
            "location_id": location_id, "event_id": None,
            "type": type_, "severity": severity, "priority": 4,
            "title": (f"{label_prefix}{variable} threshold exceeded — {loc_name}: "
                      f"{value:.1f}{unit} (threshold {threshold:.1f}{unit})"),
            "message": (f"Observed/forecast {variable} of {value:.1f}{unit} meets or "
                        f"exceeds your threshold of {threshold:.1f}{unit} for {loc_name}. "
                        f"Based on collected forecast data for {day}, not a prediction of "
                        f"any other hazard."),
            "source": "aether-threshold", "kind": "system_analysis",
            "issued_at": utc_iso(), "expires_at": utc_iso(now_utc() + timedelta(hours=24)),
            "confidence": "medium", "dedup_key": dedup_key,
        }
        if _issue(conn, alert):
            n += 1

    stats = _latest_run_stats(conn, location_id)
    if stats:
        checks = [
            ("temp_high_c", stats.get("tmax"), "WEATHER", "temperature-high", "°C", "advisory"),
            ("temp_low_c", stats.get("tmin"), "WEATHER", "temperature-low", "°C", "advisory"),
            ("rain_mm_h", stats.get("pmax"), "WEATHER", "rain-rate", " mm/h", "advisory"),
            ("wind_kmh", stats.get("wmax"), "WEATHER", "wind-speed", " km/h", "advisory"),
            ("aqi", stats.get("aqimax"), "AIR_QUALITY", "aqi", "", "advisory"),
        ]
        for field, value, type_, variable, unit, sev in checks:
            thr = pref.get(field)
            if thr is None or value is None:
                continue
            if field == "temp_low_c":
                hit = value <= thr
            else:
                hit = value >= thr
            if hit:
                maybe_issue(type_, variable, value, thr, unit, sev)

    # quake-magnitude threshold: real detected quakes near the location, last 48h
    qthr = pref.get("quake_mag")
    if qthr is not None:
        loc = conn.execute("SELECT latitude, longitude FROM locations WHERE id = ?",
                           (location_id,)).fetchone()
        if loc:
            radius = pref.get("radius_km") or 100.0
            for r in conn.execute(
                    """SELECT canonical_event_id, magnitude, region, latitude, longitude, origin_time
                       FROM earthquake_events
                       WHERE magnitude >= ? AND origin_time >= strftime('%Y-%m-%dT%H:%M:%SZ','now', '-2 days')""",
                    (qthr,)).fetchall():
                r = dict(r)
                if r.get("latitude") is None:
                    continue
                d = _haversine_km(loc["latitude"], loc["longitude"],
                                  r["latitude"], r["longitude"])
                if d <= radius:
                    dedup_key = make_dedup_key(
                        "aether-threshold", "EARTHQUAKE", "advisory",
                        event_id=r["canonical_event_id"], location_id=location_id,
                        window=day)
                    alert = {
                        "alert_id": f"thr-{location_id}-quake-{r['canonical_event_id']}-{day}",
                        "location_id": location_id,
                        "event_id": r["canonical_event_id"],
                        "type": "EARTHQUAKE", "severity": "advisory", "priority": 4,
                        "title": (f"{label_prefix}M{r['magnitude']} earthquake within "
                                  f"{radius:.0f} km of {loc_name} — {r.get('region')}"),
                        "message": (f"Detected M{r['magnitude']} at {r.get('origin_time')}, "
                                    f"{d:.0f} km from {loc_name} ({r.get('region')}), meets your "
                                    f"magnitude threshold of {qthr}. Source: USGS. Detection of a "
                                    f"past event, not a prediction."),
                        "source": "aether-threshold", "kind": "verified_observation",
                        "issued_at": r.get("origin_time") or utc_iso(),
                        "expires_at": utc_iso(now_utc() + timedelta(hours=48)),
                        "confidence": "high", "dedup_key": dedup_key,
                    }
                    if _passes_gate(pref, "EARTHQUAKE", "advisory") and _issue(conn, alert):
                        n += 1
    return n


def alerts_from_thresholds(conn: sqlite3.Connection) -> int:
    """Evaluate all enabled user threshold preferences (spec §48)."""
    n = 0
    for pref in get_prefs(conn):
        if not pref.get("enabled"):
            continue
        if pref.get("location_id") is None:
            continue
        try:
            n += _threshold_alerts_for(conn, pref["location_id"], pref)
        except Exception as e:
            log.exception("threshold alerts failed for %s", pref.get("location_id"))
    return n


def alerts_from_travel(conn: sqlite3.Connection, user_id: str = "local") -> int:
    """Temporary monitoring for an active travel destination (spec §99)."""
    travel = get_travel(conn, user_id)
    if not travel_active(travel):
        return 0
    loc_id = _nearest_location_id(conn, travel["latitude"], travel["longitude"])
    if loc_id is None:
        return 0
    # Travel uses moderate default thresholds (documented in NOTIFICATIONS.md).
    pref = {"radius_km": 300.0, "hazard_types": None, "min_severity": "LOW",
            "temp_high_c": 40.0, "temp_low_c": 0.0, "rain_mm_h": 20.0,
            "wind_kmh": 60.0, "aqi": 150.0, "quake_mag": 4.5, "enabled": 1}
    label = f"[Travel — {travel['destination_name']}] "
    try:
        return _threshold_alerts_for(conn, loc_id, pref, label_prefix=label)
    except Exception:
        log.exception("travel alerts failed")
        return 0


def build_alerts(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Run all alert builders. Returns counts per source.

    Signature kept stable for the hourly cron (scripts/run_collect.py)."""
    sm.ensure_tables(conn)
    prefs_ensure(conn)
    expired = _expire_old(conn)
    archived = sm.archive_old(conn)
    out = {"expired_marked": expired, "archived": archived}
    for name, fn in (("tsunami", alerts_from_tsunami),
                     ("earthquakes", alerts_from_earthquakes),
                     ("thresholds", alerts_from_thresholds),
                     ("travel", alerts_from_travel)):
        try:
            out[name] = fn(conn)
        except Exception as e:
            log.exception("%s alerts failed", name)
            out[f"{name}_error"] = str(e)
    log.info("alerts built: %s", out)
    return out
