"""FCM / notification tests (spec §14).

Hard rules under test:
- send() without credentials raises FCMNotConfigured (clear message),
  records notification_status='not_configured' and a 'skipped' history row —
  it NEVER reports delivered/sent when nothing was sent.
- Notification payload answers what/where/when/severity/official?/update/link.
- Silent hours suppress ordinary alerts but life-safety breaks through.
- Threshold evaluation issues alerts from real data only.
"""
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
ROOT = os.path.join(os.path.dirname(__file__), "..")

from backend.alerting import engine as alert_engine  # noqa: E402
from backend.alerting.preferences import (get_prefs, in_silent_hours,  # noqa: E402
                                          may_notify, set_prefs)
from backend.common.timeutil import utc_iso  # noqa: E402
from backend.notify import channels as chmod  # noqa: E402
from backend.notify import history as hist  # noqa: E402
from backend.notify.fcm import (FCMNotConfigured, build_payload,  # noqa: E402
                                dispatch_pending, is_configured,
                                refresh_token, register_token, send,
                                tokens_for_user)


def _memdb():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    with open(os.path.join(ROOT, "backend", "db", "schema.sql"), encoding="utf-8") as fh:
        conn.executescript(fh.read())
    return conn


def _seed_alert(conn, alert_id="a1", severity="warning", kind="official"):
    from backend.alerting import state_machine as sm
    sm.ensure_tables(conn)
    conn.execute(
        """INSERT INTO alerts
           (alert_id, type, severity, priority, title, message, source, kind,
            issued_at, status, dedup_key, version, state, confidence,
            notification_status)
           VALUES (?, 'TSUNAMI', ?, 1, 'Official tsunami message: TSUNAMI WARNING',
                   'msg', 'nws', ?, ?, 'active', ?, 1, 'SEVERITY_ASSESSED',
                   'high', 'pending')""",
        (alert_id, severity, kind, utc_iso(), "dk-" + alert_id),
    )
    conn.commit()


@pytest.fixture(autouse=True)
def _no_fcm_env(monkeypatch):
    monkeypatch.delenv("AETHER_FCM_CREDENTIALS_FILE", raising=False)


def test_send_without_creds_raises_clean_error():
    conn = _memdb()
    _seed_alert(conn)
    with pytest.raises(FCMNotConfigured, match="FCM not configured"):
        send(conn, "a1")
    # recorded honestly — never "delivered"
    row = conn.execute("SELECT notification_status FROM alerts WHERE alert_id='a1'").fetchone()
    assert row["notification_status"] == "not_configured"
    h = hist.list_history(conn)
    assert len(h) == 1
    assert h[0]["status"] == "skipped"
    assert "FCM not configured" in (h[0]["error"] or "")
    assert h[0]["status"] not in ("sent", "delivered")
    conn.close()


def test_is_configured_reports_honestly():
    info = is_configured()
    assert info["configured"] is False
    assert info["env_var"] == "AETHER_FCM_CREDENTIALS_FILE"


def test_send_missing_alert():
    conn = _memdb()
    from backend.notify.fcm import FCMError
    with pytest.raises(FCMError):
        send(conn, "ghost")
    conn.close()


def test_token_register_and_refresh():
    conn = _memdb()
    r = register_token(conn, "u1", "token-abc-123456", platform="android")
    assert r["device_id"].startswith("dev-")
    toks = tokens_for_user(conn, "u1")
    assert len(toks) == 1 and toks[0]["push_token"] == "token-abc-123456"
    assert refresh_token(conn, r["device_id"], "token-xyz-999999")
    toks = tokens_for_user(conn, "u1")
    assert toks[0]["push_token"] == "token-xyz-999999"
    assert not refresh_token(conn, "nope", "x")
    conn.close()


def test_dispatch_pending_marks_not_configured():
    conn = _memdb()
    _seed_alert(conn, "a1")
    _seed_alert(conn, "a2", severity="info", kind="system_analysis")
    out = dispatch_pending(conn)
    assert out["not_configured"] == 2
    assert out["dispatched"] == 0
    rows = conn.execute("SELECT notification_status FROM alerts").fetchall()
    assert {r["notification_status"] for r in rows} == {"not_configured"}
    conn.close()


def test_payload_answers_what_where_when_severity_official_link():
    alert = {"alert_id": "tsunami-t1", "event_id": "e1", "type": "TSUNAMI",
             "severity": "critical", "kind": "official", "source": "nws",
             "title": "Official tsunami message: TSUNAMI WARNING — Samoa",
             "message": "m", "issued_at": "2026-10-05T10:00:00Z",
             "updated_at": "2026-10-05T10:30:00Z", "version": 2}
    p = build_payload(alert, "tok")
    assert "Samoa" in p["notification"]["title"]          # what + where
    assert "2026-10-05T10:00:00Z" in p["notification"]["body"]  # when
    assert "LIFE_SAFETY" in p["notification"]["body"] or "SEVERE" in p["notification"]["body"]  # severity
    assert "OFFICIAL" in p["notification"]["body"]        # official?
    assert "10:30:00" in p["notification"]["body"]        # latest update
    assert p["data"]["deep_link"] == "aether://alerts/tsunami-t1"  # details link
    assert p["data"]["official"] == "1"
    assert p["android"]["notification"]["channel_id"] == "aether_safety"
    # analysis kind at same severity must NOT use the safety channel
    p2 = build_payload({**alert, "kind": "system_analysis"}, "tok")
    assert p2["data"]["official"] == "0"


def test_channel_hierarchy_official_outranks():
    assert chmod.channel_for("critical", "official") == "aether_safety"
    assert chmod.channel_for("warning", "official") == "aether_safety"
    assert chmod.channel_for("warning", "system_analysis") == "aether_hazard"
    assert chmod.channel_for("info", "system_analysis") == "aether_info"


def test_silent_hours_suppress_but_life_safety_breaks_through():
    now = datetime.now(timezone.utc)
    start = (now - timedelta(hours=1)).strftime("%H:%M")
    end = (now + timedelta(hours=1)).strftime("%H:%M")
    prefs = {"silent_start": start, "silent_end": end,
             "silent_days": None, "silent_tz": None,
             "silent_override_life_safety": 1}
    assert in_silent_hours(prefs, now)
    assert not may_notify("warning", prefs, now)      # suppressed
    assert not may_notify("info", prefs, now)         # suppressed
    assert may_notify("critical", prefs, now)         # life-safety breaks through
    # override disabled -> even critical suppressed
    prefs2 = dict(prefs, silent_override_life_safety=0)
    assert not may_notify("critical", prefs2, now)
    # no silent hours configured -> everything passes
    assert may_notify("info", {}, now)


def test_dispatch_respects_silent_hours():
    conn = _memdb()
    from backend.alerting import state_machine as sm
    sm.ensure_tables(conn)
    _seed_alert(conn, "a1", severity="warning", kind="official")
    now = datetime.now(timezone.utc)
    set_prefs(conn, "local",
              silent_start=(now - timedelta(hours=1)).strftime("%H:%M"),
              silent_end=(now + timedelta(hours=1)).strftime("%H:%M"),
              silent_override_life_safety=1)
    out = dispatch_pending(conn, user_id="local")
    assert out["suppressed_silent"] == 1
    row = conn.execute("SELECT notification_status FROM alerts WHERE alert_id='a1'").fetchone()
    assert row["notification_status"] == "suppressed_silent"
    conn.close()


def test_threshold_evaluation_from_real_data():
    conn = _memdb()
    now = utc_iso()
    conn.execute(
        "INSERT INTO locations (id, name, latitude, longitude, created_at)"
        " VALUES (1,'Testville',16.8,96.15,'2026-01-01T00:00:00Z')")
    run = now
    for h in range(6):
        vt = (datetime.now(timezone.utc) + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            """INSERT INTO weather_forecasts
               (location_id, source, model, forecast_run, forecast_hour, issued_at,
                valid_time, temperature, wind_speed, collected_at)
               VALUES (1,'open-meteo','gfs',?,?,?, ?, 41.5, 10.0, ?)""",
            (run, h, run, vt, now))
    conn.commit()
    set_prefs(conn, "local", location_id=1, temp_high_c=40.0, enabled=1)
    n = alert_engine.alerts_from_thresholds(conn)
    assert n == 1
    row = conn.execute("SELECT type, kind, priority, severity FROM alerts").fetchone()
    assert row["type"] == "WEATHER" and row["kind"] == "system_analysis"
    assert row["priority"] == 4
    # below threshold -> no alert
    conn.execute("DELETE FROM alerts")
    conn.commit()
    set_prefs(conn, "local", location_id=1, temp_high_c=45.0, enabled=1)
    assert alert_engine.alerts_from_thresholds(conn) == 0
    conn.close()


def test_history_read_unread():
    conn = _memdb()
    n1 = hist.record(conn, alert_id="a1", channel="push", title="t", status="sent")
    n2 = hist.record(conn, alert_id="a2", channel="push", title="t", status="sent")
    assert hist.unread_count(conn) == 2
    assert hist.mark_read(conn, n1)
    assert hist.unread_count(conn) == 1
    only = hist.list_history(conn, unread_only=True)
    assert [r["id"] for r in only] == [n2]
    assert hist.mark_all_read(conn) == 1
    assert hist.unread_count(conn) == 0
    conn.close()
