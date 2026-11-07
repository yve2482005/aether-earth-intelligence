"""One-off backfill: store the missed Oct-24 PTWC bulletins (#1 threat,
#2 passed) into the live DB through the same parse + deduped-store path
the collector uses. Safe to re-run: identical text is skipped.

Usage:
    python scripts/backfill_tsunami_ptwc.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.collectors.tsunami.message_parser import parse_official_text  # noqa: E402
from backend.collectors.tsunami.warning_sources import _store_ptwc_message  # noqa: E402

BASE = "https://www.tsunami.gov/events/PHEB/2026/10/24/26297000"
MESSAGES = [
    (1, f"{BASE}/1/WEPA40/WEPA40.txt", "tests/fixtures/tsunami/wepa40_msg1.txt"),
    (2, f"{BASE}/2/WEPA40/WEPA40.txt", "tests/fixtures/tsunami/wepa40_msg2.txt"),
]


def main() -> int:
    conn = sqlite3.connect(ROOT / "data" / "aether.db", timeout=120)
    conn.row_factory = sqlite3.Row
    for num, url, fixture in MESSAGES:
        text = (ROOT / fixture).read_text(encoding="utf-8")
        parsed = parse_official_text(text, source="ptwc", source_url=url)
        res = _store_ptwc_message(conn, parsed)
        print(f"msg#{num}: {res} event={parsed['tsunami_event_id']} "
              f"status={parsed['status']} issue={parsed['issue_time']} "
              f"type={parsed['message_type']}")
    ev = conn.execute(
        "SELECT tsunami_event_id, status, issue_time FROM tsunami_events "
        "WHERE tsunami_event_id='ptwc-26297000'").fetchone()
    print("event row:", dict(ev) if ev else None)
    print("messages:", conn.execute(
        "SELECT COUNT(*) FROM tsunami_messages "
        "WHERE tsunami_event_id='ptwc-26297000'").fetchone()[0])
    print("tsunami_messages total:", conn.execute(
        "SELECT COUNT(*) FROM tsunami_messages").fetchone()[0])
    print("active tsunami alerts:", conn.execute(
        "SELECT COUNT(*) FROM alerts WHERE type='TSUNAMI' AND status='active'"
        ).fetchone()[0])
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
