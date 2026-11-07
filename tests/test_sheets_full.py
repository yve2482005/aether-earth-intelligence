"""Tests for the full Sheets mirror: full-mirror tab selection, per-tab
guard, catalog contents, daily-archive day-guard + cell-budget splitting,
and the training-pairs workbook version guard. The gws CLI layer is fully
mocked — no live Google calls."""

import importlib.util
import json
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from backend.integrations import sheets, sheets_archive  # noqa: E402
from backend.integrations.sheets import ROW_GUARD, TAB_MAP  # noqa: E402
from backend.db import db as dbmod  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "sync_sheets_mod",
    os.path.join(os.path.dirname(__file__), "..", "scripts", "sync_sheets.py"))
sync_sheets = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sync_sheets)


# ----------------------------------------------------------------- fake CLI
class FakeGWS:
    """In-memory stand-in for hatch_gws_cli (sheets + drive subsets)."""

    def __init__(self):
        self.books = {}        # id -> {"title": str, "tabs": {title: {...}}}
        self._next_id = 0
        self.calls = []
        self._add_book("main-sid", "AETHER Control Center")

    def _add_book(self, sid, title):
        self.books[sid] = {
            "title": title,
            "tabs": {"Sheet1": {"sheetId": 0, "values": []}},
        }
        return sid

    def __call__(self, *args):
        self.calls.append(args)
        if args[:2] == ("sheets", "status"):
            return {"status": "connected"}
        params = {}
        body = {}
        if "--params" in args:
            params = json.loads(args[args.index("--params") + 1])
        if "--json" in args:
            body = json.loads(args[args.index("--json") + 1])
        if args[:3] == ("drive", "files", "list"):
            q = params.get("q", "")
            name = q.split("'")[1] if "'" in q else ""
            files = [{"id": sid, "name": b["title"]}
                     for sid, b in self.books.items() if b["title"] == name]
            return {"files": files}
        if args[1:3] == ("spreadsheets", "create"):
            self._next_id += 1
            sid = "book-%d" % self._next_id
            self._add_book(sid, body["properties"]["title"])
            return {"spreadsheetId": sid,
                    "properties": {"title": body["properties"]["title"]}}
        sid = params.get("spreadsheetId")
        book = self.books.get(sid)
        if args[1:3] == ("spreadsheets", "get"):
            if "sheets(properties" in params.get("fields", ""):
                return {"sheets": [
                    {"properties": {"title": t, "sheetId": v["sheetId"]}}
                    for t, v in book["tabs"].items()]}
            return {"properties": {"title": book["title"]}}
        if args[1:3] == ("spreadsheets", "batchUpdate"):
            for req in body.get("requests", []):
                if "addSheet" in req:
                    title = req["addSheet"]["properties"]["title"]
                    book["tabs"][title] = {
                        "sheetId": len(book["tabs"]) * 100 + 7, "values": []}
                elif "updateSheetProperties" in req:
                    props = req["updateSheetProperties"]["properties"]
                    for t, v in list(book["tabs"].items()):
                        if v["sheetId"] == props["sheetId"] and "title" in props:
                            book["tabs"][props["title"]] = book["tabs"].pop(t)
            return {}
        if args[1:4] == ("spreadsheets", "values", "clear"):
            tab = params["range"].split("!")[0]
            book["tabs"].setdefault(tab, {"sheetId": 999, "values": []})
            book["tabs"][tab]["values"] = []
            return {}
        if args[1:4] == ("spreadsheets", "values", "update"):
            tab, cell = params["range"].rsplit("!", 1)
            start_row = int(cell[1:])
            book["tabs"].setdefault(tab, {"sheetId": 999, "values": []})
            values = book["tabs"][tab]["values"]
            for i, row in enumerate(body["values"]):
                r = start_row - 1 + i
                while len(values) <= r:
                    values.append([])
                values[r] = row
            return {"updatedCells": sum(len(r) for r in body["values"])}
        if args[1:4] == ("spreadsheets", "values", "get"):
            tab = params["range"].split("!")[0]
            return {"values": book["tabs"].get(tab, {}).get("values", [])}
        raise AssertionError("unexpected cli call: %s" % (args,))

    # helpers for assertions
    def book_by_title(self, title):
        for sid, b in self.books.items():
            if b["title"] == title:
                return sid, b
        return None, None

    def count_calls(self, *prefix):
        return sum(1 for c in self.calls if c[:len(prefix)] == prefix)


@pytest.fixture()
def fake(monkeypatch, tmp_path):
    g = FakeGWS()
    monkeypatch.setattr(sheets, "_cli", g)
    monkeypatch.setattr(sheets_archive, "_cli", g)
    monkeypatch.setattr(sheets, "_connected_cache", True)
    monkeypatch.setattr(sheets_archive, "STATE_FILE",
                        tmp_path / "SHEETS_ARCHIVE_STATE.json")
    monkeypatch.delenv("AETHER_SHEETS_SPREADSHEET_ID", raising=False)
    monkeypatch.setattr(sheets, "BOOTSTRAP_SHEET_ID",
                        tmp_path / "SHEETS_ID.txt")
    (tmp_path / "SHEETS_ID.txt").write_text("main-sid\n")
    return g


def _mk_db(tmp_path, ddl_statements, inserts):
    path = str(tmp_path / "t.db")
    conn = dbmod.connect(path)
    for ddl in ddl_statements:
        conn.execute(ddl)
    for sql, rows in inserts:
        conn.executemany(sql, rows)
    conn.commit()
    return conn


FORECAST_DDL = ("CREATE TABLE weather_forecasts (id INTEGER PRIMARY KEY, "
                "location_id INTEGER, model TEXT, valid_time TEXT, "
                "temperature REAL, collected_at TEXT)")
UPDATES_DDL = ("CREATE TABLE earthquake_updates (id INTEGER PRIMARY KEY, "
               "event_id TEXT, received_at TEXT)")


# --------------------------------------------------------------------- tests
def test_full_mirror_tab_selection():
    for t in ("weather_forecasts", "earthquake_updates"):
        assert t not in TAB_MAP
    for t in ("locations", "weather_daily", "ocean_forecasts",
              "model_versions", "ml_batch_predictions", "ingestion_runs",
              "data_quality", "training_runs", "tsunami_messages",
              "system_health", "earthquake_events"):
        assert t in TAB_MAP
    assert TAB_MAP["weather_observations"] == "observations"
    assert TAB_MAP["source_registry"] == "source_health"
    assert sheets.CATALOG_TAB in sheets.REQUIRED_TABS
    assert ROW_GUARD == 45000


def test_guard_returns_newest_rows(monkeypatch, tmp_path):
    conn = _mk_db(tmp_path,
                  ["CREATE TABLE system_health (id INTEGER PRIMARY KEY, v TEXT)"],
                  [("INSERT INTO system_health (id, v) VALUES (?, ?)",
                    [(i, "r%d" % i) for i in range(1, 13)])])
    rows, total, truncated = sync_sheets.full_rows(conn, "system_health",
                                                   guard=5)
    assert total == 12 and truncated is True
    assert [r["id"] for r in rows] == [8, 9, 10, 11, 12]
    rows, total, truncated = sync_sheets.full_rows(conn, "system_health",
                                                   guard=50)
    assert total == 12 and truncated is False and len(rows) == 12
    rows, total, truncated = sync_sheets.full_rows(conn, "nope_table")
    assert rows == [] and total == 0


def test_catalog_contents(tmp_path):
    conn = _mk_db(tmp_path,
                  ["CREATE TABLE locations (id INTEGER PRIMARY KEY, name TEXT)",
                   FORECAST_DDL, UPDATES_DDL],
                  [("INSERT INTO locations VALUES (?, ?)", [(1, "Yangon")]),
                   ("INSERT INTO weather_forecasts VALUES (?,?,?,?,?,?)",
                    [(1, 1, "gfs_seamless", "2026-10-17T00:00:00Z", 30.0,
                      "2026-10-16T01:00:00Z")]),
                   ("INSERT INTO earthquake_updates VALUES (?,?,?)",
                    [(1, "e1", "2026-10-16T01:00:00Z")])])
    stats = {"locations": {"sheet_rows": 1, "truncated": False}}
    state = {"archived_days": {"2026-10-16": {
        "forecast_rows": 1, "update_rows": 1, "workbooks": ["AETHER_Data_2026-10-16"]}}}
    training_info = {"pair_rows": 42, "dataset_version": "ds_x",
                     "status": "ok"}
    rows = sync_sheets.build_catalog(conn, stats, state, training_info,
                                     "2026-10-17T00:00:00Z")
    by = {r["dataset"]: r for r in rows}
    assert by["locations"]["location"] == "main workbook tab 'locations'"
    assert by["locations"]["sheet_rows"] == 1
    assert "AETHER_Data_2026-10-16" in by["weather_forecasts"]["location"]
    assert by["weather_forecasts"]["sheet_rows"] == 1
    assert by["earthquake_updates"]["db_rows"] == 1
    assert by["training_pairs"]["location"].endswith("AETHER_Training_Data'")
    assert "ds_x" in by["training_pairs"]["note"]


def test_catalog_marks_truncation(tmp_path):
    conn = _mk_db(tmp_path,
                  ["CREATE TABLE system_health (id INTEGER PRIMARY KEY)"],
                  [("INSERT INTO system_health VALUES (?)", [(1,)])])
    stats = {"system_health": {"sheet_rows": ROW_GUARD, "truncated": True}}
    rows = sync_sheets.build_catalog(conn, stats, {}, {}, "t")
    row = [r for r in rows if r["dataset"] == "system_health"][0]
    assert "TRUNCATED" in row["note"]


def test_archive_day_guard_idempotent(fake, tmp_path):
    conn = _mk_db(tmp_path, [FORECAST_DDL, UPDATES_DDL], [
        ("INSERT INTO weather_forecasts VALUES (?,?,?,?,?,?)",
         [(i, 1, m, "2026-10-15T0%d:00:00Z" % i, 30.0,
           "2026-10-15T01:00:00Z")
          for i, m in enumerate(
              ["gfs_seamless", "gfs_seamless", "icon_seamless"], start=1)]),
        ("INSERT INTO earthquake_updates VALUES (?,?,?)",
         [(1, "e1", "2026-10-15T02:00:00Z"),
          (2, "e2", "2026-10-15T03:00:00Z")]),
    ])
    out = sheets_archive.archive_pending_days(conn, today="2026-10-16")
    assert out["status"] == "ok" and out["days_archived"] == 1
    assert out["forecast_rows"] == 3 and out["update_rows"] == 2
    sid, book = fake.book_by_title("AETHER_Data_2026-10-15")
    assert sid is not None
    assert set(book["tabs"]) >= {"gfs_seamless", "icon_seamless",
                                 "earthquake_updates"}
    # header + 2 data rows in the gfs tab
    assert len(book["tabs"]["gfs_seamless"]["values"]) == 3
    assert book["tabs"]["gfs_seamless"]["values"][0][2] == "model"

    creates = fake.count_calls("sheets", "spreadsheets", "create")
    updates = fake.count_calls("sheets", "spreadsheets", "values", "update")
    # Second run the same day: day-guard -> nothing rewritten.
    out2 = sheets_archive.archive_pending_days(conn, today="2026-10-16")
    assert out2["days_archived"] == 0
    assert fake.count_calls("sheets", "spreadsheets", "create") == creates
    assert fake.count_calls("sheets", "spreadsheets", "values",
                            "update") == updates
    # Today itself is never archived (day not complete).
    out3 = sheets_archive.archive_pending_days(conn, today="2026-10-15")
    assert out3["days_archived"] == 0


def test_archive_cell_budget_splits_workbooks(fake, tmp_path, monkeypatch):
    monkeypatch.setattr(sheets_archive, "CELL_BUDGET", 30)
    rows = [(i, 1, m, "2026-10-15T00:00:00Z", 30.0, "2026-10-15T01:00:00Z")
            for i, m in enumerate(
                ["gfs_seamless"] * 3 + ["icon_seamless"] * 3, start=1)]
    conn = _mk_db(tmp_path, [FORECAST_DDL, UPDATES_DDL], [
        ("INSERT INTO weather_forecasts VALUES (?,?,?,?,?,?)", rows),
        ("INSERT INTO earthquake_updates VALUES (?,?,?)",
         [(1, "e1", "2026-10-15T02:00:00Z")]),
    ])
    out = sheets_archive.archive_pending_days(conn, today="2026-10-16")
    assert out["days_archived"] == 1 and out["forecast_rows"] == 6
    _, b1 = fake.book_by_title("AETHER_Data_2026-10-15")
    _, b2 = fake.book_by_title("AETHER_Data_2026-10-15_2")
    assert b1 is not None and b2 is not None
    written = 0
    for b in (b1, b2):
        for tab, v in b["tabs"].items():
            if tab in ("gfs_seamless", "icon_seamless"):
                written += len(v["values"]) - 1  # minus header
    assert written == 6


def _fake_dataset(n=6, version="ds_fake1"):
    from ml.weather.dataset import (
        REGIME_CODE, TASKS, WeatherDataset, feature_names, iso_to_epoch)
    names = feature_names()
    X = np.zeros((n, len(names)))
    y = {t: np.arange(n, dtype=float) for t in TASKS}
    return WeatherDataset(
        X=X, feature_names=names, y=y,
        regimes=np.full(n, REGIME_CODE["clear"], dtype=np.int8),
        valid_times=np.array(
            [iso_to_epoch("2026-10-1%dT00:00:00Z" % (i % 10))
             for i in range(n)], dtype=np.int64),
        location_ids=np.array([1 + (i % 2) for i in range(n)], dtype=np.int64),
        forecast_hours=np.zeros(n), dataset_version=version,
        params={}, n_pairs=n, time_min="2026-10-10T00:00:00Z",
        time_max="2026-10-15T00:00:00Z")


def test_training_version_guard(fake, tmp_path, monkeypatch):
    ds = _fake_dataset()
    monkeypatch.setattr("ml.weather.dataset.build_dataset",
                        lambda db_path: ds)
    out = sheets_archive.sync_training_workbook(db_path="ignored.db")
    assert out["status"] == "ok" and out["pair_rows"] == 6
    sid, book = fake.book_by_title("AETHER_Training_Data")
    assert sid is not None
    assert len(book["tabs"]["pairs"]["values"]) == 7  # header + 6
    reg = {r[0]: r[1] for r in book["tabs"]["registry"]["values"][1:]}
    assert reg["dataset_version"] == "ds_fake1"
    assert reg["pair_rows_total"] == "6"
    assert reg["locations_total"] == "2"

    updates = fake.count_calls("sheets", "spreadsheets", "values", "update")
    out2 = sheets_archive.sync_training_workbook(db_path="ignored.db")
    assert out2["status"] == "skipped"
    assert fake.count_calls("sheets", "spreadsheets", "values",
                            "update") == updates


def _tab_update_calls(fake, since=0):
    """{tab: count} of values.update calls in fake.calls[since:]."""
    counts = {}
    for args in fake.calls[since:]:
        if args[1:4] == ("spreadsheets", "values", "update"):
            params = json.loads(args[args.index("--params") + 1])
            tab = params["range"].split("!")[0]
            counts[tab] = counts.get(tab, 0) + 1
    return counts


def _day_conn(tmp_path):
    return _mk_db(tmp_path, [FORECAST_DDL, UPDATES_DDL], [
        ("INSERT INTO weather_forecasts VALUES (?,?,?,?,?,?)",
         [(1, 1, "gfs_seamless", "2026-10-15T00:00:00Z", 30.0,
           "2026-10-15T01:00:00Z"),
          (2, 1, "gfs_seamless", "2026-10-15T01:00:00Z", 31.0,
           "2026-10-15T01:00:00Z"),
          (3, 1, "icon_seamless", "2026-10-15T00:00:00Z", 29.0,
           "2026-10-15T01:00:00Z")]),
        ("INSERT INTO earthquake_updates VALUES (?,?,?)",
         [(1, "e1", "2026-10-15T02:00:00Z"),
          (2, "e2", "2026-10-15T03:00:00Z")]),
    ])


def _flaky_write_batched(monkeypatch, fail_tab):
    """Make the first write of `fail_tab` report an error, then heal."""
    real = sheets_archive.write_batched
    state = {"failed": False}

    def flaky(sid, tab, headers, rows):
        if tab == fail_tab and not state["failed"]:
            state["failed"] = True
            return {"status": "error", "tab": tab, "reason": "boom",
                    "rows": 0}
        return real(sid, tab, headers, rows)

    monkeypatch.setattr(sheets_archive, "write_batched", flaky)


def test_archive_checkpoint_resume(fake, tmp_path, monkeypatch):
    # Tabs are written in plan order: earthquake_updates, then the model
    # tabs alphabetically. Fail gfs (the 2nd tab) on the first run.
    _flaky_write_batched(monkeypatch, "gfs_seamless")
    conn = _day_conn(tmp_path)
    out = sheets_archive.archive_pending_days(conn, today="2026-10-16")
    assert out["days_failed"] == 1 and out["days_archived"] == 0

    # The first tab is checkpointed in the state file; the day is not
    # promoted, and its partial work survives in the workbook.
    saved = json.loads(sheets_archive.STATE_FILE.read_text())
    assert "2026-10-15" not in saved["archived_days"]
    prog = saved["in_progress_days"]["2026-10-15"]["tabs"]
    assert prog == {"AETHER_Data_2026-10-15:earthquake_updates": {"rows": 2}}
    _, book = fake.book_by_title("AETHER_Data_2026-10-15")
    assert len(book["tabs"]["earthquake_updates"]["values"]) == 3

    # Second run: only the two remaining tabs are pushed.
    mark = len(fake.calls)
    out2 = sheets_archive.archive_pending_days(conn, today="2026-10-16")
    assert out2["days_archived"] == 1
    day_result = [d for d in out2["days"] if d["day"] == "2026-10-15"][0]
    assert day_result["resumed_tabs"] == 1
    pushed = _tab_update_calls(fake, mark)
    assert "earthquake_updates" not in pushed
    assert pushed.get("gfs_seamless", 0) >= 1
    assert pushed.get("icon_seamless", 0) >= 1

    # Promotion: archived entry has exactly the long-standing shape and
    # lists every tab (checkpointed + freshly written); in-progress gone.
    saved = json.loads(sheets_archive.STATE_FILE.read_text())
    entry = saved["archived_days"]["2026-10-15"]
    assert set(entry) == {"workbooks", "forecast_rows", "update_rows",
                          "tabs", "archived_at"}
    assert entry["forecast_rows"] == 3 and entry["update_rows"] == 2
    assert entry["tabs"] == [
        "AETHER_Data_2026-10-15:earthquake_updates",
        "AETHER_Data_2026-10-15:gfs_seamless",
        "AETHER_Data_2026-10-15:icon_seamless",
    ]
    assert "2026-10-15" not in saved["in_progress_days"]
    _, book = fake.book_by_title("AETHER_Data_2026-10-15")
    assert len(book["tabs"]["gfs_seamless"]["values"]) == 3  # header + 2
    assert len(book["tabs"]["icon_seamless"]["values"]) == 2  # header + 1


def test_archive_checkpoint_count_mismatch_repushes(fake, tmp_path,
                                                   monkeypatch):
    # Fail the LAST tab (icon) so updates + gfs get checkpointed.
    _flaky_write_batched(monkeypatch, "icon_seamless")
    conn = _day_conn(tmp_path)
    out = sheets_archive.archive_pending_days(conn, today="2026-10-16")
    assert out["days_failed"] == 1
    # A late update row arrives for the same day: the recorded count (2)
    # no longer matches what would be pushed (3) -> tab is re-pushed,
    # while the still-matching gfs checkpoint is honoured.
    conn.execute("INSERT INTO earthquake_updates VALUES (?,?,?)",
                 (3, "e3", "2026-10-15T04:00:00Z"))
    conn.commit()
    mark = len(fake.calls)
    out2 = sheets_archive.archive_pending_days(conn, today="2026-10-16")
    assert out2["days_archived"] == 1
    pushed = _tab_update_calls(fake, mark)
    assert pushed.get("earthquake_updates", 0) >= 1
    assert "gfs_seamless" not in pushed
    assert pushed.get("icon_seamless", 0) >= 1
    saved = json.loads(sheets_archive.STATE_FILE.read_text())
    assert saved["archived_days"]["2026-10-15"]["update_rows"] == 3
    _, book = fake.book_by_title("AETHER_Data_2026-10-15")
    assert len(book["tabs"]["earthquake_updates"]["values"]) == 4


def test_archive_old_state_file_compatible(fake, tmp_path):
    # A pre-checkpoint state file: no in_progress_days / mirror sections.
    old = {
        "workbooks": {"AETHER_Data_2026-10-14": "book-old"},
        "archived_days": {"2026-10-14": {
            "workbooks": ["AETHER_Data_2026-10-14"], "forecast_rows": 5,
            "update_rows": 1, "tabs": ["AETHER_Data_2026-10-14:gfs_seamless"],
            "archived_at": "2026-10-15T00:00:00Z"}},
        "training": {},
    }
    sheets_archive.STATE_FILE.write_text(json.dumps(old))
    state = sheets_archive.load_state()
    assert state["in_progress_days"] == {} and state["mirror"] == {}
    conn = _day_conn(tmp_path)
    out = sheets_archive.archive_pending_days(conn, today="2026-10-16")
    assert out["days_archived"] == 1
    saved = json.loads(sheets_archive.STATE_FILE.read_text())
    # The pre-existing day entry and workbook id survive untouched.
    assert saved["archived_days"]["2026-10-14"] == old["archived_days"]["2026-10-14"]
    assert saved["workbooks"]["AETHER_Data_2026-10-14"] == "book-old"
    assert saved["archived_days"]["2026-10-15"]["forecast_rows"] == 3


def test_mirror_signature_candidates(tmp_path):
    conn = _mk_db(tmp_path, [
        "CREATE TABLE locations (id INTEGER PRIMARY KEY, name TEXT,"
        " created_at TEXT)",
        "CREATE TABLE saved_locations (user_id INTEGER,"
        " location_id INTEGER, label TEXT)",
    ], [
        ("INSERT INTO locations VALUES (?,?,?)",
         [(1, "A", "2026-10-15T00:00:00Z"), (2, "B", "2026-10-16T00:00:00Z")]),
        ("INSERT INTO saved_locations VALUES (?,?,?)", [(1, 1, "home")]),
    ])
    sig = sync_sheets.mirror_signature(conn, "locations")
    assert sig == {"rows": 2, "max_rowid": 2, "ts_col": "created_at",
                   "ts_max": "2026-10-16T00:00:00Z"}
    sig2 = sync_sheets.mirror_signature(conn, "saved_locations")
    assert sig2["rows"] == 1 and sig2["ts_col"] is None
    assert sync_sheets.mirror_signature(conn, "nope_table") is None


def test_mirror_skips_unchanged_and_repushes_on_change(fake, tmp_path):
    conn = _mk_db(tmp_path, [
        "CREATE TABLE locations (id INTEGER PRIMARY KEY, name TEXT,"
        " created_at TEXT)",
    ], [
        ("INSERT INTO locations VALUES (?,?,?)",
         [(1, "Yangon", "2026-10-15T00:00:00Z"),
          (2, "Mandalay", "2026-10-15T01:00:00Z")]),
    ])
    mirror = sheets.SheetMirror()
    assert mirror.enabled

    stats = sync_sheets.mirror_tables(conn, mirror)
    assert stats["locations"]["status"] == "ok"
    assert _tab_update_calls(fake).get("locations", 0) >= 1
    saved = json.loads(sheets_archive.STATE_FILE.read_text())
    sig = saved["mirror"]["tabs"]["locations"]["signature"]
    assert sig["rows"] == 2 and sig["ts_col"] == "created_at"
    assert saved["mirror"]["tabs"]["locations"]["tab"] == "locations"

    # Unchanged table: skipped, no values.update for the tab.
    mark = len(fake.calls)
    stats2 = sync_sheets.mirror_tables(conn, mirror)
    assert stats2["locations"]["status"] == "skipped_unchanged"
    assert stats2["locations"]["sheet_rows"] == 2
    assert "locations" not in _tab_update_calls(fake, mark)

    # A new row changes the signature: pushed again.
    conn.execute("INSERT INTO locations VALUES (?,?,?)",
                 (3, "Naypyidaw", "2026-10-16T00:00:00Z"))
    conn.commit()
    mark = len(fake.calls)
    stats3 = sync_sheets.mirror_tables(conn, mirror)
    assert stats3["locations"]["status"] == "ok"
    assert _tab_update_calls(fake, mark).get("locations", 0) >= 1
    saved = json.loads(sheets_archive.STATE_FILE.read_text())
    assert saved["mirror"]["tabs"]["locations"]["signature"]["rows"] == 3


def test_sync_phase_order_archive_first():
    # The archive child must run before the mirror, and training after:
    # guard the ordering in main() against accidental reverts.
    src = open(os.path.join(os.path.dirname(__file__), "..",
                            "scripts", "sync_sheets.py")).read()
    main_src = src[src.index("def main()"):]
    i_archive = main_src.index('_run_phase("archive"')
    i_mirror = main_src.index("mirror_tables(conn, mirror)")
    i_training = main_src.index('_run_phase("training"')
    assert i_archive < i_mirror < i_training


def test_training_cell_budget_chunks_by_location(fake, tmp_path, monkeypatch):
    ds = _fake_dataset(n=8)
    monkeypatch.setattr("ml.weather.dataset.build_dataset",
                        lambda db_path: ds)
    # 8 rows x 28 cols = ~224 cells; force a split by location group.
    monkeypatch.setattr(sheets_archive, "CELL_BUDGET", 150)
    out = sheets_archive.sync_training_workbook(db_path="ignored.db")
    assert out["status"] == "ok" and out["pair_rows"] == 8
    assert len(out["workbooks"]) == 2
    total = 0
    for part in out["workbooks"]:
        _, book = fake.book_by_title(part["workbook"])
        total += len(book["tabs"]["pairs"]["values"]) - 1
    assert total == 8  # every pair stored, none truncated
    _, first = fake.book_by_title("AETHER_Training_Data")
    reg = {r[0]: r[1] for r in first["tabs"]["registry"]["values"][1:]}
    assert reg["workbook_part"] == "1 of 2"
    assert "AETHER_Training_Data_2" in reg["tab_layout"]
