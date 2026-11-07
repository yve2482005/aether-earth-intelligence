"""Regression tests: NWS tsunami TEST transmissions must never escalate.

Real incident (2026-10-20): the NWS transmission-time test event
nws-68f99fa5d1efaa23 — titled 'TEST Tsunami Warning issued ...' with body
'...THIS_MESSAGE_IS_FOR_TEST_PURPOSES_ONLY...' — was stored as an active
'TSUNAMI WARNING' and raised a priority-1 critical alert. Test messages
reuse the real category wording, so the parser must detect the test
markers, store them with status='test' / severity='TEST' (original
wording preserved verbatim), and the alert engine must skip them —
including legacy rows stored before the fix.
"""

import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
ROOT = os.path.join(os.path.dirname(__file__), "..")

from backend.alerting import engine as alert_engine  # noqa: E402
from backend.alerting.severity import official_to_severity  # noqa: E402
from backend.collectors.tsunami.message_parser import (  # noqa: E402
    is_test_message, parse_atom_entry, parse_official_text)

# Verbatim title of the real stored test event nws-68f99fa5d1efaa23.
TEST_TITLE = ("TEST Tsunami Warning issued October 20 at 12:31PM EDT until "
              "October 20 at 1:31PM EDT by NWS National Tsunami Warning Center")
# Verbatim closing lines of that message's real body (underscores as sent).
TEST_BODY_TAIL = ("...THIS_MESSAGE_IS_FOR_TEST_PURPOSES_ONLY...\n\n"
                  "...THIS IS A TEST TO DETERMINE TRANSMISSION TIMES "
                  "INVOLVED IN THE DISSEMINATION OF TSUNAMI INFORMATION...")
GENUINE_TITLE = ("Tsunami Warning issued October 20 at 12:31PM EDT until "
                 "October 20 at 1:31PM EDT by NWS National Tsunami Warning Center")


def test_is_test_message_detects_real_test_title():
    assert is_test_message(TEST_TITLE) is True


def test_is_test_message_detects_underscored_purpose_phrase():
    assert is_test_message("Tsunami Warning", TEST_BODY_TAIL) is True


def test_is_test_message_passes_genuine_warning():
    assert is_test_message(GENUINE_TITLE, "A tsunami warning is in effect.") is False


def test_parse_atom_entry_marks_real_test_message():
    parsed = parse_atom_entry({
        "title": TEST_TITLE,
        "summary": TEST_BODY_TAIL,
        "id": "urn:test:1",
        "updated": "2026-10-20T16:31:00Z",
        "link": "https://example.invalid/1",
    })
    assert parsed["status"] == "test"
    assert parsed["severity"] == "TEST"
    # Original classification preserved verbatim — never rewritten.
    assert parsed["message_type"] == "TSUNAMI WARNING"
    assert parsed["raw_classification"] == TEST_TITLE
    assert official_to_severity(parsed["severity"]) != "critical"


def test_parse_atom_entry_cap_status_test():
    parsed = parse_atom_entry({
        "title": GENUINE_TITLE,
        "summary": "",
        "id": "urn:test:2",
        "updated": "2026-10-20T16:31:00Z",
        "cap_status": "Test",
    })
    assert parsed["status"] == "test"
    assert parsed["severity"] == "TEST"


def test_parse_atom_entry_genuine_warning_stays_active_critical():
    parsed = parse_atom_entry({
        "title": GENUINE_TITLE,
        "summary": "A tsunami warning is in effect for the coast.",
        "id": "urn:test:3",
        "updated": "2026-10-20T16:31:00Z",
        "cap_status": "Actual",
    })
    assert parsed["status"] == "active"
    assert parsed["severity"] == "TSUNAMI WARNING"
    assert official_to_severity(parsed["severity"]) == "critical"


def test_parse_official_text_marks_test_and_genuine():
    test_parsed = parse_official_text(f"{TEST_TITLE}\n\n{TEST_BODY_TAIL}")
    assert test_parsed["status"] == "test"
    assert test_parsed["severity"] == "TEST"
    genuine = parse_official_text(f"{GENUINE_TITLE}\n\nEvacuate to high ground.")
    assert genuine["status"] == "active"
    assert official_to_severity(genuine["severity"]) == "critical"


def _memdb():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    with open(os.path.join(ROOT, "backend", "db", "schema.sql"),
              encoding="utf-8") as fh:
        conn.executescript(fh.read())
    return conn


def _insert_event(conn, event_id, status, raw_classification):
    conn.execute(
        """INSERT INTO tsunami_events
           (tsunami_event_id, source, message_type, severity, status,
            raw_classification, issue_time, updated_at)
           VALUES (?, 'nws', 'TSUNAMI WARNING', 'TSUNAMI WARNING', ?, ?,
                   '2026-10-20T16:31:00Z', '2026-10-20T16:31:00Z')""",
        (event_id, status, raw_classification))
    conn.commit()


def test_engine_skips_test_and_legacy_rows_but_alerts_genuine():
    conn = _memdb()
    _insert_event(conn, "nws-testrow", "test", TEST_TITLE)
    # Legacy row: stored before the parser fix, still status='active'.
    _insert_event(conn, "nws-legacytest", "active", TEST_TITLE)
    _insert_event(conn, "nws-genuine", "active", GENUINE_TITLE)
    n = alert_engine.alerts_from_tsunami(conn)
    assert n == 1
    rows = conn.execute("SELECT alert_id, severity, priority FROM alerts").fetchall()
    assert [r["alert_id"] for r in rows] == ["tsunami-nws-genuine"]
    assert rows[0]["severity"] == "critical"
    assert rows[0]["priority"] == 1
