"""Alert dedup tests (spec §13).

- make_dedup_key stays byte-compatible (existing dedup behavior preserved).
- event_merge_key ignores severity so "M6.2 -> M6.4 -> M6.5" merges.
- Engine: magnitude revisions of the same event = ONE alert row with
  revisions; notification re-arms only on a meaningful severity change.
- Re-running builders with no changes is idempotent (no revision spam).
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
ROOT = os.path.join(os.path.dirname(__file__), "..")

from backend.alerting import engine as alert_engine  # noqa: E402
from backend.alerting import state_machine as sm  # noqa: E402
from backend.alerting.dedup import (event_merge_key, make_dedup_key,  # noqa: E402
                                    same_event)
from backend.alerting.severity import is_meaningful_escalation  # noqa: E402
from backend.common.timeutil import utc_iso  # noqa: E402


def _memdb():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    with open(os.path.join(ROOT, "backend", "db", "schema.sql"), encoding="utf-8") as fh:
        conn.executescript(fh.read())
    return conn


def _seed_quake(conn, mag, eid="q1"):
    now = utc_iso()
    conn.execute(
        """INSERT INTO earthquake_events
           (canonical_event_id, source, source_event_ids, origin_time,
            latitude, longitude, depth, magnitude, magnitude_type, region, created_at)
           VALUES (?,'usgs','["us1"]',?,?,?,?,?,'mw','Yangon region',?)""",
        (eid, now, 16.8, 96.1, 10.0, mag, now),
    )
    conn.commit()


def test_make_dedup_key_stable_and_compatible():
    k1 = make_dedup_key("usgs", "EARTHQUAKE", "warning", event_id="q1")
    k2 = make_dedup_key("usgs", "EARTHQUAKE", "warning", event_id="q1")
    assert k1 == k2 and len(k1) == 32
    # severity is part of the legacy key (behavior preserved)
    k3 = make_dedup_key("usgs", "EARTHQUAKE", "critical", event_id="q1")
    assert k3 != k1


def test_event_merge_key_ignores_severity():
    m1 = event_merge_key("usgs", "EARTHQUAKE", event_id="q1")
    m2 = event_merge_key("usgs", "EARTHQUAKE", event_id="q1")
    assert m1 == m2
    assert m1 != event_merge_key("usgs", "EARTHQUAKE", event_id="q2")
    assert m1 != event_merge_key("usgs", "TSUNAMI", event_id="q1")


def test_same_event_matching():
    a = {"event_id": "q1", "type": "EARTHQUAKE", "source": "usgs",
         "location_id": 1}
    b = dict(a, severity="warning")  # severity differs -> still same event
    assert same_event(a, b)
    assert not same_event(a, {**b, "event_id": "q2"})
    assert not same_event(a, {**b, "type": "TSUNAMI"})
    assert not same_event(a, {**b, "source": "emsc"})
    assert not same_event(a, {**b, "location_id": 2})
    assert not same_event(a, {**b, "event_id": None})


def test_magnitude_revisions_merge_into_one_alert():
    conn = _memdb()
    _seed_quake(conn, 6.2)
    out1 = alert_engine.build_alerts(conn)
    assert out1["earthquakes"] == 1
    for mag in (6.4, 6.5):
        conn.execute("UPDATE earthquake_events SET magnitude=? WHERE canonical_event_id='q1'",
                     (mag,))
        conn.commit()
        alert_engine.build_alerts(conn)
    rows = conn.execute("SELECT * FROM alerts").fetchall()
    assert len(rows) == 1, "M6.2 -> M6.4 -> M6.5 must be ONE event"
    row = dict(rows[0])
    assert row["version"] == 3
    assert "M6.5" in row["title"]
    revs = sm.revisions(conn, "quake-q1")
    ups = [r for r in revs if r["to_state"] == "UPDATED"]
    assert len(ups) == 2
    conn.close()


def test_escalation_only_on_meaningful_severity_change():
    conn = _memdb()
    _seed_quake(conn, 6.2)  # maps to advisory
    alert_engine.build_alerts(conn)
    row = conn.execute("SELECT notification_status FROM alerts").fetchone()
    assert row["notification_status"] == "pending"

    # 6.2 -> 6.4: same mapped severity (advisory) -> revision, no escalation
    conn.execute("UPDATE earthquake_events SET magnitude=6.4 WHERE canonical_event_id='q1'")
    conn.commit()
    alert_engine.build_alerts(conn)
    row = conn.execute("SELECT notification_status, severity FROM alerts").fetchone()
    assert row["severity"] == "advisory"
    assert row["notification_status"] == "pending"

    # 6.4 -> 6.6: advisory -> warning -> escalation re-arms notification
    conn.execute("UPDATE earthquake_events SET magnitude=6.6 WHERE canonical_event_id='q1'")
    conn.commit()
    alert_engine.build_alerts(conn)
    row = conn.execute("SELECT notification_status, severity FROM alerts").fetchone()
    assert row["severity"] == "warning"
    assert row["notification_status"] == "escalation_pending"
    conn.close()


def test_escalation_predicate():
    assert is_meaningful_escalation("advisory", "warning")
    assert is_meaningful_escalation("warning", "critical")
    assert is_meaningful_escalation("info", "critical")
    assert not is_meaningful_escalation("warning", "warning")
    assert not is_meaningful_escalation("warning", "advisory")
    assert not is_meaningful_escalation("critical", "warning")


def test_idempotent_rerun_records_no_revisions():
    conn = _memdb()
    _seed_quake(conn, 6.5)
    alert_engine.build_alerts(conn)
    n_revs = len(sm.revisions(conn, "quake-q1"))
    out = alert_engine.build_alerts(conn)
    assert out["earthquakes"] == 0
    assert len(sm.revisions(conn, "quake-q1")) == n_revs
    assert conn.execute("SELECT COUNT(*) c FROM alerts").fetchone()["c"] == 1
    conn.close()
