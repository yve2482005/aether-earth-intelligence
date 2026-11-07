"""Alert priority-ordering and dedup tests.

Contract (schema.sql + CONTRACTS.md + backend/alerting/engine.py):
  priority 1 = official life-safety warning, 2 = verified observation of a
  significant hazard, 3 = official advisory/watch or high-confidence analysis,
  4 = medium-confidence analysis, 5 = low-confidence analysis.
  Rule: an official warning MUST outrank lower-confidence analysis for the
  same event. alerts.dedup_key is UNIQUE; re-running the builders must not
  create duplicate active alerts (INSERT ... ON CONFLICT DO NOTHING).

The reference priority/order tests below pin the contract; the
`test_backend_*` tests run the real engine against an in-memory SQLite DB
built from backend/db/schema.sql.
"""

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
ROOT = os.path.join(os.path.dirname(__file__), "..")

try:
    from backend.alerting import engine as alert_engine
    from backend.common.timeutil import utc_iso

    BACKEND_LANDED = True
except Exception:
    BACKEND_LANDED = False

PRIORITY = {"official": 1, "verified_observation": 2, "system_analysis": 5}


def priority_of(alert):
    """Reference implementation of the documented priority ladder."""
    kind = alert.get("kind")
    if kind == "official" and alert.get("severity") in ("warning", "critical"):
        return 1
    if kind == "official":
        return 3
    return PRIORITY.get(kind, 5)


def order(alerts):
    return sorted(alerts, key=lambda a: (priority_of(a), a.get("issued_at", "")))


def test_official_warning_outranks_low_confidence_analysis():
    analysis = {"kind": "system_analysis", "severity": "warning",
                "title": "model analysis", "issued_at": "2026-10-05T10:00:00Z"}
    official = {"kind": "official", "severity": "warning",
                "title": "NWS tsunami warning", "issued_at": "2026-10-05T12:00:00Z"}
    ranked = order([analysis, official])
    assert ranked[0]["kind"] == "official"


def test_official_info_does_not_outrank_life_safety():
    official_info = {"kind": "official", "severity": "info",
                     "issued_at": "2026-10-05T13:00:00Z"}
    official_warning = {"kind": "official", "severity": "warning",
                        "issued_at": "2026-10-05T14:00:00Z"}
    ranked = order([official_info, official_warning])
    assert ranked[0]["severity"] == "warning"


def test_verified_observation_between_official_and_analysis():
    obs = {"kind": "verified_observation", "severity": "advisory",
           "issued_at": "2026-10-05T11:00:00Z"}
    official = {"kind": "official", "severity": "advisory",
                "issued_at": "2026-10-05T14:00:00Z"}
    analysis = {"kind": "system_analysis", "severity": "warning",
                "issued_at": "2026-10-05T10:00:00Z"}
    ranked = order([analysis, obs, official])
    # verified observation of a significant hazard (2) outranks an official
    # advisory (3); both outrank low-confidence analysis (5)
    assert [priority_of(a) for a in ranked] == [2, 3, 5]


def _memdb():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    with open(os.path.join(ROOT, "backend", "db", "schema.sql"), encoding="utf-8") as fh:
        conn.executescript(fh.read())
    return conn


@pytest.mark.skipif(not BACKEND_LANDED,
                    reason="backend/alerting/engine.py not landed yet (backend team)")
def test_backend_official_tsunami_warning_gets_priority_1():
    conn = _memdb()
    now = utc_iso()
    conn.execute(
        """INSERT INTO tsunami_events
           (tsunami_event_id, source, message_type, raw_classification,
            issue_time, status, updated_at)
           VALUES ('t1','nws','TSUNAMI WARNING','Tsunami Warning',?, 'active', ?)""",
        (now, now),
    )
    conn.commit()
    out = alert_engine.build_alerts(conn)
    assert out.get("tsunami", 0) == 1
    row = conn.execute("SELECT priority, kind, severity FROM alerts").fetchone()
    assert row["priority"] == 1
    assert row["kind"] == "official"
    assert row["severity"] == "critical"
    conn.close()


@pytest.mark.skipif(not BACKEND_LANDED,
                    reason="backend/alerting/engine.py not landed yet (backend team)")
def test_backend_significant_quake_gets_priority_2_verified_observation():
    conn = _memdb()
    now = utc_iso()
    conn.execute(
        """INSERT INTO earthquake_events
           (canonical_event_id, source, source_event_ids, origin_time,
            latitude, longitude, depth, magnitude, magnitude_type, region, created_at)
           VALUES ('q1','usgs','["us1"]',?,?,?,?,?, 'mw','Yangon region', ?)""",
        (now, 16.8, 96.1, 10.0, 6.5, now),
    )
    conn.commit()
    out = alert_engine.build_alerts(conn)
    assert out.get("earthquakes", 0) == 1
    row = conn.execute("SELECT priority, kind FROM alerts").fetchone()
    assert row["priority"] == 2
    assert row["kind"] == "verified_observation"
    conn.close()


@pytest.mark.skipif(not BACKEND_LANDED,
                    reason="backend/alerting/engine.py not landed yet (backend team)")
def test_backend_builders_are_idempotent_via_dedup_key():
    conn = _memdb()
    now = utc_iso()
    conn.execute(
        """INSERT INTO tsunami_events
           (tsunami_event_id, source, message_type, issue_time, status, updated_at)
           VALUES ('t1','nws','TSUNAMI WARNING',?, 'active', ?)""",
        (now, now),
    )
    conn.commit()
    first = alert_engine.build_alerts(conn)
    second = alert_engine.build_alerts(conn)
    assert first.get("tsunami", 0) == 1
    assert second.get("tsunami", 0) == 0  # ON CONFLICT DO NOTHING
    count = conn.execute("SELECT COUNT(*) AS c FROM alerts").fetchone()["c"]
    assert count == 1
    keys = [r[0] for r in conn.execute("SELECT dedup_key FROM alerts")]
    assert len(keys) == len(set(keys))
    conn.close()


@pytest.mark.skipif(not BACKEND_LANDED,
                    reason="backend/alerting/engine.py not landed yet (backend team)")
def test_backend_official_outranks_quake_observation_in_listing():
    conn = _memdb()
    now = utc_iso()
    conn.execute(
        """INSERT INTO tsunami_events
           (tsunami_event_id, source, message_type, issue_time, status, updated_at)
           VALUES ('t1','nws','TSUNAMI WARNING',?, 'active', ?)""",
        (now, now),
    )
    conn.execute(
        """INSERT INTO earthquake_events
           (canonical_event_id, source, source_event_ids, origin_time,
            latitude, longitude, magnitude, created_at)
           VALUES ('q1','usgs','["us1"]',?, 16.8, 96.1, 6.5, ?)""",
        (now, now),
    )
    conn.commit()
    alert_engine.build_alerts(conn)
    rows = conn.execute(
        "SELECT kind, priority FROM alerts ORDER BY priority ASC").fetchall()
    assert [(r["kind"], r["priority"]) for r in rows] == [
        ("official", 1), ("verified_observation", 2)]
    conn.close()
