"""Alert state machine tests (spec §12).

States: DETECTED -> VALIDATING -> CONFIRMED -> SEVERITY_ASSESSED ->
ALERTED -> UPDATED -> RESOLVED -> ARCHIVED.
Illegal transitions must be rejected (InvalidTransition), every transition
must be recorded, and build_alerts() output must enter at DETECTED.
"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
ROOT = os.path.join(os.path.dirname(__file__), "..")

from backend.alerting import engine as alert_engine  # noqa: E402
from backend.alerting import state_machine as sm  # noqa: E402
from backend.common.timeutil import utc_iso  # noqa: E402


def _memdb():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    with open(os.path.join(ROOT, "backend", "db", "schema.sql"), encoding="utf-8") as fh:
        conn.executescript(fh.read())
    return conn


def _seed_quake(conn, mag=6.2, eid="q1"):
    now = utc_iso()
    conn.execute(
        """INSERT INTO earthquake_events
           (canonical_event_id, source, source_event_ids, origin_time,
            latitude, longitude, depth, magnitude, magnitude_type, region, created_at)
           VALUES (?,'usgs','["us1"]',?,?,?,?,?,'mw','Yangon region',?)""",
        (eid, now, 16.8, 96.1, 10.0, mag, now),
    )
    conn.commit()


def _insert_raw_alert(conn, alert_id="a1"):
    sm.ensure_tables(conn)
    conn.execute(
        """INSERT INTO alerts
           (alert_id, type, severity, priority, title, message, source, kind,
            issued_at, status, dedup_key, version, state, confidence,
            notification_status)
           VALUES (?, 'EARTHQUAKE', 'warning', 2, 't', 'm', 'usgs',
                   'verified_observation', ?, 'active', ?, 1, 'DETECTED',
                   'high', 'pending')""",
        (alert_id, utc_iso(), "dk-" + alert_id),
    )
    conn.commit()


def test_new_alert_enters_at_detected_and_bootstraps():
    conn = _memdb()
    _seed_quake(conn)
    alert_engine.build_alerts(conn)
    row = conn.execute(
        "SELECT state, version, notification_status, confidence FROM alerts"
    ).fetchone()
    assert row["state"] == "SEVERITY_ASSESSED"
    assert row["version"] == 1
    assert row["notification_status"] == "pending"
    assert row["confidence"] == "high"
    revs = sm.revisions(conn, "quake-q1")
    assert [(r["from_state"], r["to_state"]) for r in revs] == [
        ("DETECTED", "VALIDATING"),
        ("VALIDATING", "CONFIRMED"),
        ("CONFIRMED", "SEVERITY_ASSESSED"),
    ]
    assert all(r["actor"] == "alert-engine" for r in revs)
    conn.close()


def test_legal_transition_chain():
    conn = _memdb()
    _insert_raw_alert(conn)
    sm.transition(conn, "a1", "VALIDATING", "ok")
    sm.transition(conn, "a1", "CONFIRMED", "ok")
    sm.transition(conn, "a1", "SEVERITY_ASSESSED", "ok")
    sm.transition(conn, "a1", "ALERTED", "ok")
    sm.transition(conn, "a1", "UPDATED", "rev1", bump_version=True)
    sm.transition(conn, "a1", "UPDATED", "rev2", bump_version=True)
    sm.transition(conn, "a1", "RESOLVED", "done")
    sm.transition(conn, "a1", "ARCHIVED", "old")
    row = conn.execute("SELECT state, version FROM alerts WHERE alert_id='a1'").fetchone()
    assert row["state"] == "ARCHIVED"
    assert row["version"] == 3  # two bumped revisions
    assert len(sm.revisions(conn, "a1")) == 8
    conn.close()


@pytest.mark.parametrize("bad", [
    ("DETECTED", "CONFIRMED"),      # skip
    ("DETECTED", "ALERTED"),
    ("VALIDATING", "SEVERITY_ASSESSED"),
    ("SEVERITY_ASSESSED", "DETECTED"),  # backward
    ("ALERTED", "CONFIRMED"),
    ("RESOLVED", "UPDATED"),
    ("ARCHIVED", "RESOLVED"),       # terminal
    ("ARCHIVED", "DETECTED"),
])
def test_illegal_transitions_rejected(bad):
    conn = _memdb()
    _insert_raw_alert(conn)
    frm, to = bad
    conn.execute("UPDATE alerts SET state=? WHERE alert_id='a1'", (frm,))
    conn.commit()
    with pytest.raises(sm.InvalidTransition):
        sm.transition(conn, "a1", to, "should fail")
    # state unchanged, no revision recorded
    row = conn.execute("SELECT state FROM alerts WHERE alert_id='a1'").fetchone()
    assert row["state"] == frm
    conn.close()


def test_unknown_state_rejected():
    conn = _memdb()
    _insert_raw_alert(conn)
    with pytest.raises(sm.InvalidTransition):
        sm.transition(conn, "a1", "MELTED", "nope")
    conn.close()


def test_missing_alert_rejected():
    conn = _memdb()
    sm.ensure_tables(conn)
    with pytest.raises(sm.InvalidTransition):
        sm.transition(conn, "ghost", "VALIDATING", "nope")
    conn.close()


def test_expiry_moves_to_resolved():
    conn = _memdb()
    _seed_quake(conn)
    alert_engine.build_alerts(conn)
    conn.execute("UPDATE alerts SET expires_at='2020-01-01T00:00:00Z'")
    conn.commit()
    out = alert_engine.build_alerts(conn)
    assert out["expired_marked"] == 1
    row = conn.execute("SELECT status, state FROM alerts").fetchone()
    assert row["status"] == "expired"
    assert row["state"] == "RESOLVED"
    revs = sm.revisions(conn, "quake-q1")
    assert revs[-1]["to_state"] == "RESOLVED"
    assert revs[-1]["reason"] == "expired"
    conn.close()


def test_archive_old_resolved():
    conn = _memdb()
    _insert_raw_alert(conn)
    for to in ("VALIDATING", "CONFIRMED", "SEVERITY_ASSESSED", "RESOLVED"):
        sm.transition(conn, "a1", to, "t")
    conn.execute("UPDATE alerts SET issued_at='2020-01-01T00:00:00Z'")
    conn.commit()
    n = sm.archive_old(conn, days=30)
    assert n == 1
    row = conn.execute("SELECT state FROM alerts WHERE alert_id='a1'").fetchone()
    assert row["state"] == "ARCHIVED"
    # row is never deleted
    assert conn.execute("SELECT COUNT(*) c FROM alerts").fetchone()["c"] == 1
    conn.close()


def test_revision_table_falls_back_when_foreign_shape_exists():
    """schema.sql ships another workstream's alert_revisions shape; the state
    machine must not break and must not corrupt it."""
    conn = _memdb()
    # foreign shape already created by schema.sql
    cols = {r[1] for r in conn.execute("PRAGMA table_info(alert_revisions)").fetchall()}
    assert "from_state" not in cols
    _insert_raw_alert(conn)
    sm.transition(conn, "a1", "VALIDATING", "ok")
    assert sm.revision_table_name(conn) == "alert_state_revisions"
    assert len(sm.revisions(conn, "a1")) == 1
    # foreign table untouched
    assert conn.execute("SELECT COUNT(*) c FROM alert_revisions").fetchone()["c"] == 0
    conn.close()
