"""Hang-resistance tests for the Sheets transport + sync snapshot reads.

Production incident (Oct 17-18): sync_sheets intermittently wedged for
27-60 min at ~0 CPU — hatch_gws_cli calls stalled through the egress
proxy and the old subprocess.run(timeout=...) could not bound them (it
kills only the direct child, then waits for pipe EOF). These tests use a
real fake CLI executable that sleeps, and prove:

  * sheets._cli kills a hung call within a bounded time (process-group
    kill), including across its one bounded retry;
  * a hung tab fails fast via SheetMirror.push_tab and the NEXT tab
    still syncs (per-tab failure isolation);
  * sync_sheets reads from a consistent sqlite snapshot, not the live DB.
"""

import importlib.util
import os
import sqlite3
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from backend.integrations import sheets  # noqa: E402
from backend.db import db as dbmod  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "sync_sheets_timeout_mod",
    os.path.join(os.path.dirname(__file__), "..", "scripts", "sync_sheets.py"))
sync_sheets = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sync_sheets)

# Fake hatch_gws_cli: hangs (30 s sleep) whenever any argument mentions
# "hangtab"; otherwise answers with empty JSON like a healthy call.
FAKE_CLI = """#!/bin/sh
case "$*" in
  *hangtab*) sleep 30 ;;
  *) printf '{}' ;;
esac
"""


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    script = tmp_path / "fake_gws_cli"
    script.write_text(FAKE_CLI)
    script.chmod(0o755)
    monkeypatch.setattr(sheets, "GWS_CLI", str(script))
    monkeypatch.setattr(sheets, "CLI_TIMEOUT_S", 1)
    monkeypatch.setattr(sheets, "CLI_RETRY_BACKOFF_S", 0)
    return script


def test_cli_kills_hung_call_within_bound(fake_cli, monkeypatch):
    monkeypatch.setattr(sheets, "CLI_ATTEMPTS", 2)  # exercise the retry too
    started = time.time()
    with pytest.raises(RuntimeError, match="timed out"):
        sheets._cli("sheets", "spreadsheets", "values", "clear",
                    "--params", '{"range": "hangtab!A:Z"}')
    elapsed = time.time() - started
    # 2 attempts x 1 s timeout + reap grace; the sleep was 30 s per call,
    # so anything well under that proves the kill actually happened.
    assert elapsed < 15


def test_cli_healthy_call_unaffected(fake_cli):
    assert sheets._cli("sheets", "spreadsheets", "get") == {}


def test_hung_tab_fails_fast_and_next_tab_syncs(fake_cli, monkeypatch):
    monkeypatch.setattr(sheets, "CLI_ATTEMPTS", 1)
    monkeypatch.setattr(sheets, "_connected_cache", True)
    monkeypatch.setenv("AETHER_SHEETS_SPREADSHEET_ID", "sid123")
    mirror = sheets.SheetMirror()

    started = time.time()
    out = mirror.push_tab("hangtab", [{"a": 1}, {"a": 2}])
    elapsed = time.time() - started
    assert out["status"] == "error"
    assert elapsed < 15  # bounded by the per-call timeout, not the 30 s hang

    out2 = mirror.push_tab("goodtab", [{"a": 1}])
    assert out2["status"] == "ok"
    assert out2["rows"] == 1


def test_open_snapshot_reads_consistent_copy(tmp_path, monkeypatch):
    live_path = tmp_path / "live.db"
    raw = sqlite3.connect(str(live_path))
    raw.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    raw.execute("INSERT INTO t VALUES (1, 'before')")
    raw.commit()
    raw.close()

    import backend.common.config as config
    monkeypatch.setattr(dbmod, "DB_PATH", live_path)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)

    conn, snap_path = sync_sheets._open_snapshot()
    try:
        assert snap_path.parent == tmp_path
        assert conn.execute("SELECT v FROM t WHERE id=1").fetchone()[0] \
            == "before"
        # Writes to the live DB after the snapshot are NOT visible: the
        # sync reads one consistent view and holds no live-DB connection.
        other = sqlite3.connect(str(live_path))
        other.execute("UPDATE t SET v='after' WHERE id=1")
        other.commit()
        other.close()
        assert conn.execute("SELECT v FROM t WHERE id=1").fetchone()[0] \
            == "before"
    finally:
        conn.close()
        sync_sheets._remove_snapshot(snap_path)
    assert not snap_path.exists()
