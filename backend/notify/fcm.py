"""FCM push sender (spec §14) — REAL, config-gated, never faked.

Configuration:
  AETHER_FCM_CREDENTIALS_FILE  -> path to a Firebase service-account JSON.
  firebase_admin must be installed (pip install firebase-admin; zero spend —
  Firebase Cloud Messaging itself is free).

Contract (hard):
  - send() REQUIRES credentials. When absent/unreadable or firebase_admin is
    missing, send() raises FCMNotConfigured with a clear message.
  - The system records notification_status='not_configured' on the alert and a
    'skipped' history row with the reason. It NEVER reports delivered when the
    message was not sent.
  - Device tokens live in the landed `devices` table (push_token column):
    register_token() upserts, refresh_token() rotates, invalidate_token()
    clears a dead token.

No secrets are read from code or committed files — only the env var path.
"""
from __future__ import annotations

import os
import sqlite3
from typing import Any, Dict, List, Optional

from backend.common.timeutil import utc_iso
from backend.notify import channels as chmod
from backend.notify.history import ensure_tables as history_ensure, record as history_record

CREDS_ENV = "AETHER_FCM_CREDENTIALS_FILE"


class FCMNotConfigured(Exception):
    """Raised when FCM cannot send: no credentials, bad file, or missing lib."""


class FCMError(Exception):
    """Raised when FCM send was attempted and failed."""


def creds_path() -> Optional[str]:
    p = os.environ.get(CREDS_ENV, "").strip()
    return p or None


def is_configured() -> Dict[str, Any]:
    """Honest config probe: returns what is present and what is missing."""
    p = creds_path()
    info: Dict[str, Any] = {"env_var": CREDS_ENV, "credentials_file": p,
                             "file_readable": False, "library": False,
                             "configured": False}
    if p:
        info["file_readable"] = os.path.isfile(p) and os.access(p, os.R_OK)
    try:
        import firebase_admin  # noqa: F401
        info["library"] = True
    except Exception:
        info["library"] = False
    info["configured"] = bool(p and info["file_readable"] and info["library"])
    return info


def _require_config() -> str:
    info = is_configured()
    if not info["configured"]:
        missing = []
        if not info["credentials_file"]:
            missing.append(f"env {CREDS_ENV} is not set")
        elif not info["file_readable"]:
            missing.append(f"credentials file {info['credentials_file']!r} is not readable")
        if not info["library"]:
            missing.append("firebase_admin is not installed")
        raise FCMNotConfigured(
            "FCM not configured: " + "; ".join(missing) +
            ". See docs/NOTIFICATIONS.md for setup steps.")
    return info["credentials_file"]  # type: ignore[return-value]


def _app():
    import firebase_admin
    from firebase_admin import credentials
    path = _require_config()
    try:
        return firebase_admin.get_app("aether")
    except ValueError:
        cred = credentials.Certificate(path)
        return firebase_admin.initialize_app(cred, name="aether")


# ---- device token registry (landed `devices` table) ----

def ensure_devices_table(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS devices (
        device_id TEXT PRIMARY KEY,
        user_id TEXT,
        platform TEXT NOT NULL,
        push_token TEXT,
        app_version TEXT,
        last_seen TEXT, created_at TEXT NOT NULL
    )""")
    conn.commit()


def register_token(conn: sqlite3.Connection, user_id: str, token: str,
                   platform: str = "android",
                   device_id: Optional[str] = None,
                   app_version: Optional[str] = None) -> Dict[str, Any]:
    """Register (or refresh the last_seen of) a device push token."""
    ensure_devices_table(conn)
    import hashlib
    device_id = device_id or ("dev-" + hashlib.sha256(token.encode()).hexdigest()[:16])
    now = utc_iso()
    # devices.user_id references users(id) with FK enforcement on: ensure row.
    conn.execute(
        "INSERT OR IGNORE INTO users (id, created_at) VALUES (?, ?)",
        (user_id, now))
    conn.execute(
        """INSERT INTO devices (device_id, user_id, platform, push_token,
                                app_version, last_seen, created_at)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(device_id) DO UPDATE SET
             push_token=excluded.push_token, user_id=excluded.user_id,
             platform=excluded.platform, app_version=excluded.app_version,
             last_seen=excluded.last_seen""",
        (device_id, user_id, platform, token, app_version, now, now))
    conn.commit()
    return {"device_id": device_id, "user_id": user_id, "platform": platform,
            "registered_at": now}


def refresh_token(conn: sqlite3.Connection, device_id: str,
                  new_token: str) -> bool:
    ensure_devices_table(conn)
    cur = conn.execute(
        "UPDATE devices SET push_token = ?, last_seen = ? WHERE device_id = ?",
        (new_token, utc_iso(), device_id))
    conn.commit()
    return cur.rowcount > 0


def invalidate_token(conn: sqlite3.Connection, token: str) -> int:
    """Clear a dead token (FCM reported NotRegistered)."""
    ensure_devices_table(conn)
    cur = conn.execute("UPDATE devices SET push_token = NULL WHERE push_token = ?",
                       (token,))
    conn.commit()
    return cur.rowcount


def tokens_for_user(conn: sqlite3.Connection,
                    user_id: Optional[str] = None) -> List[Dict[str, Any]]:
    ensure_devices_table(conn)
    sql = "SELECT device_id, user_id, platform, push_token FROM devices WHERE push_token IS NOT NULL"
    params: list = []
    if user_id:
        sql += " AND user_id = ?"
        params.append(user_id)
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


# ---- payload (spec §102: what/where/when/severity/official/update/link) ----

def build_payload(alert: Dict[str, Any], token: str,
                  deep_link: Optional[str] = None) -> Dict[str, Any]:
    """Build the FCM message dict. Content answers:
    what (title), where (region in title), when (issued time), severity,
    official? (kind tag), latest update, details link (deep link)."""
    from backend.alerting.severity import to_new_severity
    alert_id = alert.get("alert_id", "")
    sev = to_new_severity(alert.get("severity"))
    kind = alert.get("kind", "system_analysis")
    is_official = kind == "official"
    title = str(alert.get("title") or "AETHER alert")
    body_lines = [
        f"Severity: {sev}",
        f"Issued: {alert.get('issued_at') or 'unknown time'}",
        f"Source: {'OFFICIAL — ' if is_official else ''}{alert.get('source') or 'AETHER'}",
    ]
    if alert.get("updated_at"):
        body_lines.append(f"Last update: {alert['updated_at']}")
    if alert.get("version"):
        body_lines.append(f"Revision {alert['version']}")
    link = deep_link or f"aether://alerts/{alert_id}"
    body_lines.append(f"Details: {link}")
    channel = chmod.channel_for(alert.get("severity"), kind)
    return {
        "token": token,
        "notification": {"title": title, "body": "\n".join(body_lines)},
        "data": {
            "alert_id": str(alert_id),
            "event_id": str(alert.get("event_id") or ""),
            "type": str(alert.get("type") or ""),
            "severity": sev,
            "kind": kind,
            "official": "1" if is_official else "0",
            "issued_at": str(alert.get("issued_at") or ""),
            "deep_link": link,
            "click_action": "OPEN_ALERT",
        },
        "android": {
            "priority": "high" if channel == "aether_safety" else "normal",
            "notification": {
                "channel_id": channel,
                "click_action": "OPEN_ALERT",
            },
        },
    }


# ---- send ----

def _mark_alert_status(conn: sqlite3.Connection, alert_id: str,
                       status: str) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(alerts)").fetchall()}
    if "notification_status" in cols:
        conn.execute("UPDATE alerts SET notification_status = ? WHERE alert_id = ?",
                     (status, alert_id))
        conn.commit()


def send(conn: sqlite3.Connection, alert_id: str,
         user_id: Optional[str] = None,
         dry_run: bool = False) -> Dict[str, Any]:
    """Send push for one alert to all registered tokens (optionally one user).

    Raises FCMNotConfigured when credentials/library are missing — and records
    notification_status='not_configured' + a 'skipped' history row. Never
    reports delivered when the message was not sent.
    """
    history_ensure(conn)
    row = conn.execute("SELECT * FROM alerts WHERE alert_id = ?",
                       (alert_id,)).fetchone()
    if row is None:
        raise FCMError(f"alert {alert_id!r} not found")
    alert = dict(row)

    if dry_run:
        # Validate-only: build the payload without touching FCM and without
        # requiring credentials. Never reported as delivered.
        payload = build_payload(alert, "DRY_RUN_TOKEN")
        history_record(conn, alert_id=alert_id, channel="push",
                       recipient=user_id or "all",
                       title=str(alert.get("title")),
                       body=str(alert.get("message"))[:500],
                       status="skipped", error="dry_run: payload validated, not sent")
        return {"alert_id": alert_id, "dry_run": True, "status": "dry_run",
                "payload": payload}

    try:
        _require_config()
    except FCMNotConfigured as e:
        _mark_alert_status(conn, alert_id, "not_configured")
        history_record(conn, alert_id=alert_id, channel="push",
                       recipient=user_id or "all",
                       title=str(alert.get("title")),
                       body=str(alert.get("message"))[:500],
                       status="skipped", error=str(e))
        raise

    targets = tokens_for_user(conn, user_id)
    if not targets:
        _mark_alert_status(conn, alert_id, "no_tokens")
        history_record(conn, alert_id=alert_id, channel="push",
                       recipient=user_id or "all",
                       title=str(alert.get("title")),
                       body=str(alert.get("message"))[:500],
                       status="skipped", error="no registered device tokens")
        return {"alert_id": alert_id, "sent": 0, "failed": 0,
                "status": "no_tokens"}

    from firebase_admin import messaging
    app = _app()
    sent, failed, errors = 0, [], []
    for t in targets:
        payload = build_payload(alert, t["push_token"])
        msg = messaging.Message(
            token=payload["token"],
            notification=messaging.Notification(
                title=payload["notification"]["title"],
                body=payload["notification"]["body"]),
            data=payload["data"],
            android=messaging.AndroidConfig(
                priority=payload["android"]["priority"],
                notification=messaging.AndroidNotification(
                    channel_id=payload["android"]["notification"]["channel_id"],
                    click_action="OPEN_ALERT")),
        )
        try:
            mid = messaging.send(msg, app=app, dry_run=dry_run)
            sent += 1
            history_record(conn, alert_id=alert_id, channel="push",
                           recipient=t["device_id"],
                           title=payload["notification"]["title"],
                           body=payload["notification"]["body"][:500],
                           status="sent", error=None, sent_at=utc_iso(),
                           provider_message_id=mid)
        except Exception as e:
            errmsg = f"{type(e).__name__}: {e}"
            failed.append(t["device_id"])
            errors.append(errmsg)
            if "NotRegistered" in type(e).__name__ or "not-registered" in errmsg:
                invalidate_token(conn, t["push_token"])
            history_record(conn, alert_id=alert_id, channel="push",
                           recipient=t["device_id"],
                           title=payload["notification"]["title"],
                           body=payload["notification"]["body"][:500],
                           status="failed", error=errmsg[:500])

    final = "delivered" if sent and not failed else ("partial" if sent else "failed")
    _mark_alert_status(conn, alert_id, final)
    return {"alert_id": alert_id, "sent": sent, "failed": len(failed),
            "failed_devices": failed, "errors": errors[:5],
            "dry_run": dry_run, "status": final}


def dispatch_pending(conn: sqlite3.Connection,
                     user_id: Optional[str] = None) -> Dict[str, Any]:
    """Send all alerts awaiting notification, honoring silent hours per user
    preferences (spec §16). Alerts suppressed by silent hours are recorded as
    'suppressed_silent' (life-safety still breaks through when override is on).
    """
    from backend.alerting.preferences import get_prefs, may_notify
    history_ensure(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(alerts)").fetchall()}
    if "notification_status" not in cols:
        return {"dispatched": 0, "reason": "notification_status column missing"}
    rows = conn.execute(
        """SELECT * FROM alerts
           WHERE status = 'active'
             AND notification_status IN ('pending','escalation_pending')
           ORDER BY priority ASC, issued_at DESC""").fetchall()
    out = {"dispatched": 0, "suppressed_silent": 0, "not_configured": 0,
           "errors": []}
    prefs = {p.get("location_id"): p for p in get_prefs(conn, user_id or "local")}
    global_pref = prefs.get(None)
    for r in rows:
        alert = dict(r)
        pref = prefs.get(alert.get("location_id"), global_pref)
        if not may_notify(alert.get("severity"), pref):
            _mark_alert_status(conn, alert["alert_id"], "suppressed_silent")
            history_record(conn, alert_id=alert["alert_id"], channel="push",
                           recipient=user_id or "all",
                           title=str(alert.get("title")),
                           body=str(alert.get("message"))[:500],
                           status="skipped", error="suppressed: silent hours")
            out["suppressed_silent"] += 1
            continue
        try:
            res = send(conn, alert["alert_id"], user_id=user_id)
            out["dispatched"] += res.get("sent", 0)
        except FCMNotConfigured:
            out["not_configured"] += 1
        except Exception as e:  # noqa: BLE001 - keep dispatching the rest
            out["errors"].append(f"{alert['alert_id']}: {e}")
    return out
