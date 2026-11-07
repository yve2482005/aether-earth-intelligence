"""Tests for the incident system (§71), alert delivery audit (§79) and
source failover log (§§35-36). Throwaway DBs only.

The module reuses the v1 table schemas (incidents, notification_deliveries,
source_failover) plus the new additive incident_notes table.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.incidents import (  # noqa: E402
    add_timeline_note,
    delivery_stats,
    ensure_tables,
    get_incident,
    list_incidents,
    record_delivery,
    record_failover,
    record_incident,
    recent_failovers,
    resolve_incident,
)


@pytest.fixture()
def db(tmp_path):
    path = str(tmp_path / "inc.db")
    conn = sqlite3.connect(path)
    ensure_tables(conn)
    conn.close()
    yield path


def ro(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _notification(conn, alert_id="AL-1", channel="push"):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS notification_history (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " alert_id TEXT, channel TEXT, recipient TEXT, title TEXT, body TEXT,"
        " status TEXT DEFAULT 'queued', error TEXT, created_at TEXT, sent_at TEXT)")
    cur = conn.execute(
        "INSERT INTO notification_history (alert_id, channel, created_at) VALUES (?,?,datetime('now'))",
        (alert_id, channel))
    conn.commit()
    return cur.lastrowid


# ---------------- incident lifecycle ----------------

def test_incident_lifecycle(db):
    conn = ro(db)
    inc = record_incident(conn, "source_outage", "collector:open-meteo",
                          "Open-Meteo unreachable", "connect timeout x3")
    assert inc["id"].startswith("INC-")
    assert inc["type"] == "source_outage"
    assert inc["severity"] == "major"
    assert inc["status"] == "open"
    assert inc["ended_at"] is None
    # timeline starts with type + created notes
    kinds = [e["kind"] for e in inc["timeline"]]
    assert "type" in kinds and "created" in kinds

    assert add_timeline_note(conn, inc["id"], "retried, still failing") is True
    got = get_incident(conn, inc["id"])
    assert len(got["timeline"]) == len(inc["timeline"]) + 1

    assert resolve_incident(conn, inc["id"], "failover to secondary source") is True
    got = get_incident(conn, inc["id"])
    assert got["status"] == "resolved"
    assert got["ended_at"] is not None
    assert got["timeline"][-1]["kind"] == "resolution"

    # open list no longer contains it; unfiltered list does
    assert all(i["id"] != inc["id"] for i in list_incidents(conn, status="open"))
    assert any(i["id"] == inc["id"] for i in list_incidents(conn))
    conn.close()


def test_incident_type_severity_mapping(db):
    conn = ro(db)
    assert record_incident(conn, "alert_failure", "notifier", "t")["severity"] == "critical"
    assert record_incident(conn, "db_failure", "db", "t")["severity"] == "critical"
    assert record_incident(conn, "model_failure", "ml", "t")["severity"] == "major"
    assert record_incident(conn, "other", "x", "t")["severity"] == "minor"
    conn.close()


def test_incident_missing_returns_none_and_false(db):
    conn = ro(db)
    assert get_incident(conn, "INC-nope") is None
    assert add_timeline_note(conn, "INC-nope", "x") is False
    assert resolve_incident(conn, "INC-nope", "x") is False
    conn.close()


# ---------------- failover log ----------------

def test_failover_log(db):
    conn = ro(db)
    rid = record_failover(conn, "weather", "open-meteo", "meteo-norway",
                          "upstream 5xx x10")
    assert rid >= 1
    fo = recent_failovers(conn, 5)
    assert len(fo) == 1
    assert fo[0]["from_source"] == "open-meteo"
    assert fo[0]["to_source"] == "meteo-norway"
    assert fo[0]["category"] == "weather"
    conn.close()


# ---------------- delivery audit ----------------

def test_delivery_audit_records_only_what_happened(db):
    conn = ro(db)
    nid = _notification(conn, alert_id="AL-42", channel="push")
    did = record_delivery(conn, nid, "push", "delivered", provider_message_id="pm-1")
    assert did >= 1
    stats = delivery_stats(conn, days=7)
    assert stats["total"] == 1
    assert stats["by_status"] == {"delivered": 1}
    assert stats["recent"][0]["alert_id"] == "AL-42"
    # nothing fabricated: no rows for channels we never recorded
    assert "sms" not in stats["by_status"]
    conn.close()


def test_delivery_stats_empty(db):
    conn = ro(db)
    stats = delivery_stats(conn, days=7)
    assert stats["total"] == 0
    assert stats["by_status"] == {}
    assert stats["recent"] == []
    conn.close()
