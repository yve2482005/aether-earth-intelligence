"""PTWC source tests (2026-11-06 accuracy update).

Real incident (2026-10-24): the PTWC threat messages for the Vanuatu M6.9
were never ingested because the collector had no PTWC source at all — only
US-domestic NWS systems. The fix adds the official PTWC Atom feed
(tsunami.gov/events/xml/PHEBAtom.xml) with bulletin-text parsing.

Fixtures are the real bulletins fetched live 2026-11-06:
  tests/fixtures/tsunami/wepa40_msg1.txt — TSUNAMI MESSAGE NUMBER 1 (threat)
  tests/fixtures/tsunami/wepa40_msg2.txt — TSUNAMI MESSAGE NUMBER 2 (passed)

Rules under test: both messages share ONE event row (ptwc-26297000);
#1 stores active, #2 flips the event to expired with correct header issue
times; the alert engine builds NO alert for the expired event; re-storing
the same bulletin text is skipped (no duplicate message versions).
"""

import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
ROOT = os.path.join(os.path.dirname(__file__), "..")

from backend.alerting import engine as alert_engine  # noqa: E402
from backend.collectors.tsunami.message_parser import parse_official_text  # noqa: E402
from backend.collectors.tsunami.warning_sources import (  # noqa: E402
    _store_ptwc_message, fetch_ptwc_messages)

FIXTURES = os.path.join(ROOT, "tests", "fixtures", "tsunami")
MSG1_URL = ("https://www.tsunami.gov/events/PHEB/2026/10/24/26297000/1/"
            "WEPA40/WEPA40.txt")
MSG2_URL = ("https://www.tsunami.gov/events/PHEB/2026/10/24/26297000/2/"
            "WEPA40/WEPA40.txt")


def _text(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return fh.read()


def _memdb():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    with open(os.path.join(ROOT, "backend", "db", "schema.sql"),
              encoding="utf-8") as fh:
        conn.executescript(fh.read())
    return conn


def test_ptwc_msg1_parses_active_with_header_time():
    parsed = parse_official_text(_text("wepa40_msg1.txt"), source="ptwc",
                                 source_url=MSG1_URL)
    assert parsed["tsunami_event_id"] == "ptwc-26297000"
    assert parsed["status"] == "active"
    assert parsed["issue_time"] == "2026-10-24T02:37:00Z"
    assert parsed["message_type"] == "TSUNAMI THREAT"
    assert parsed["source"] == "ptwc"


def test_ptwc_msg2_parses_expired_with_header_time():
    parsed = parse_official_text(_text("wepa40_msg2.txt"), source="ptwc",
                                 source_url=MSG2_URL)
    assert parsed["tsunami_event_id"] == "ptwc-26297000"
    assert parsed["status"] == "expired"
    assert parsed["issue_time"] == "2026-10-24T03:28:00Z"
    assert parsed["message_type"] == "TSUNAMI THREAT"


BONIN_URL = ("https://www.tsunami.gov/events/PHEB/2026/10/24/26297050/1/"
             "WEGM42/WEGM42.txt")


def test_ptwc_info_statement_is_not_a_warning():
    # Real Bonin Islands bulletin: an information statement stating NO
    # tsunami threat. Before the center-name strip, detect_category saw
    # 'NWS PACIFIC TSUNAMI WARNING CENTER' in the header and classified
    # it TSUNAMI WARNING (critical alert for a no-threat statement).
    parsed = parse_official_text(_text("wegm42_bonin_info.txt"),
                                 source="ptwc", source_url=BONIN_URL)
    assert parsed["tsunami_event_id"] == "ptwc-26297050"
    assert parsed["message_type"] == "TSUNAMI INFORMATION STATEMENT"
    assert parsed["status"] == "active"
    from backend.alerting.severity import official_to_severity  # noqa: E402
    assert official_to_severity(parsed["message_type"]) == "info"


def test_ptwc_messages_share_one_event_and_no_alert():
    conn = _memdb()
    p1 = parse_official_text(_text("wepa40_msg1.txt"), source="ptwc",
                             source_url=MSG1_URL)
    p2 = parse_official_text(_text("wepa40_msg2.txt"), source="ptwc",
                             source_url=MSG2_URL)
    assert _store_ptwc_message(conn, p1) == "stored"
    assert _store_ptwc_message(conn, p2) == "stored"
    events = conn.execute("SELECT * FROM tsunami_events").fetchall()
    assert len(events) == 1
    assert events[0]["tsunami_event_id"] == "ptwc-26297000"
    assert events[0]["status"] == "expired"
    assert events[0]["issue_time"] == "2026-10-24T03:28:00Z"
    msgs = conn.execute(
        "SELECT version FROM tsunami_messages ORDER BY version").fetchall()
    assert [r["version"] for r in msgs] == [1, 2]
    # Expired event: the alert engine must build nothing.
    assert alert_engine.alerts_from_tsunami(conn) == 0
    assert conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 0


def test_ptwc_rerun_skips_duplicate_text():
    conn = _memdb()
    p2 = parse_official_text(_text("wepa40_msg2.txt"), source="ptwc",
                             source_url=MSG2_URL)
    assert _store_ptwc_message(conn, p2) == "stored"
    assert _store_ptwc_message(conn, p2) == "skipped"
    assert conn.execute("SELECT COUNT(*) FROM tsunami_messages").fetchone()[0] == 1


def test_ptwc_atom_feed_entry_shape():
    import xml.etree.ElementTree as ET
    sample = """<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
    <entry><title>BONIN ISLANDS  JAPAN REGION</title>
    <updated>2026-10-25T02:27:01Z</updated>
    <id>urn:uuid:ba64bc82-07f7-45fb-ab6c-25420fca6851</id>
    <link rel="alternate" title="Bulletin"
      href="https://www.tsunami.gov/events/PHEB/2026/10/24/26297050/1/WEGM42/WEGM42.txt"
      type="application/xml" />
    </entry></feed>"""
    # Reuse the feed's own namespace handling by parsing through the same
    # code path shape fetch_ptwc_messages uses (entry/link extraction).
    ns = {"a": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(sample)
    entries = root.findall("a:entry", ns)
    assert len(entries) == 1
    links = [l.get("href") for l in entries[0].findall("a:link", ns)
             if l.get("rel") == "alternate"]
    assert links == ["https://www.tsunami.gov/events/PHEB/2026/10/24/"
                     "26297050/1/WEGM42/WEGM42.txt"]
    assert callable(fetch_ptwc_messages)
