"""Unit tests for backend/integrations/sheets.py (connected-account transport, mocked CLI)."""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from backend.integrations import sheets  # noqa: E402
from backend.integrations.sheets import (  # noqa: E402
    REQUIRED_TABS,
    TAB_MAP,
    SheetMirror,
    resolve_spreadsheet_id,
)


@pytest.fixture(autouse=True)
def _reset(monkeypatch, tmp_path):
    monkeypatch.delenv("AETHER_SHEETS_SPREADSHEET_ID", raising=False)
    monkeypatch.setattr(sheets, "_connected_cache", None)
    monkeypatch.setattr(sheets, "BOOTSTRAP_SHEET_ID", tmp_path / "SHEETS_ID.txt")
    yield
    monkeypatch.setattr(sheets, "_connected_cache", None)


def _mock_cli(monkeypatch, connected=True, existing_tabs=()):
    calls = []

    def fake_cli(*args):
        calls.append(args)
        if args[:2] == ("sheets", "status"):
            return {"status": "connected" if connected else "not_connected"}
        if args[1:3] == ("spreadsheets", "get"):
            return {"sheets": [{"properties": {"title": t}} for t in existing_tabs],
                    "properties": {"title": "AETHER Control Center"}}
        if args[1:4] == ("spreadsheets", "values", "clear"):
            return {}
        if args[1:4] == ("spreadsheets", "values", "update"):
            return {"updatedCells": 10}
        if args[1:3] == ("spreadsheets", "batchUpdate"):
            return {}
        raise AssertionError("unexpected cli call: %s" % (args,))

    monkeypatch.setattr(sheets, "_cli", fake_cli)
    return calls


def test_disabled_when_no_spreadsheet_id(monkeypatch):
    _mock_cli(monkeypatch, connected=True)
    m = SheetMirror()
    assert m.enabled is False
    h = m.health()
    assert h["component"] == "sheets"
    assert h["status"] == "not_configured"


def test_disabled_when_cli_not_connected(monkeypatch):
    _mock_cli(monkeypatch, connected=False)
    monkeypatch.setenv("AETHER_SHEETS_SPREADSHEET_ID", "sid123")
    m = SheetMirror()
    assert m.enabled is False
    assert m.health()["status"] == "not_configured"


def test_spreadsheet_id_from_bootstrap_file(monkeypatch, tmp_path):
    _mock_cli(monkeypatch, connected=True)
    (tmp_path / "SHEETS_ID.txt").write_text("boot-sid-9\n")
    assert resolve_spreadsheet_id() == "boot-sid-9"
    assert SheetMirror().enabled is True


def test_tab_map_covers_spec_section_26():
    # Full-mirror design (2026-10-17): the spec section 26 control tabs are
    # a subset of the mirrored tabs (forecasts moved to the daily archive,
    # so no `weather_data`); every mapped tab must be a required tab.
    assert {
        "observations", "earthquake_events", "tsunami_events",
        "model_scores", "alerts", "training_runs", "source_health",
    } <= set(REQUIRED_TABS)
    assert "catalog" in REQUIRED_TABS
    assert "weather_forecasts" not in TAB_MAP
    assert "earthquake_updates" not in TAB_MAP
    for table, tab in TAB_MAP.items():
        assert tab in REQUIRED_TABS


def test_ensure_tabs_creates_missing(monkeypatch):
    _mock_cli(monkeypatch, connected=True, existing_tabs=("alerts",))
    monkeypatch.setenv("AETHER_SHEETS_SPREADSHEET_ID", "sid123")
    res = SheetMirror().ensure_tabs()
    assert res["alerts"] == "exists"
    assert res["observations"] == "created"
    assert len(res) == len(REQUIRED_TABS)


def test_push_summary_rewrites_tab(monkeypatch):
    calls = _mock_cli(monkeypatch, connected=True, existing_tabs=tuple(REQUIRED_TABS))
    monkeypatch.setenv("AETHER_SHEETS_SPREADSHEET_ID", "sid123")
    rows = [{"a": 1, "b": "x"}, {"a": 2, "b": None}]
    res = SheetMirror().push_summary("alerts", rows)
    assert res["status"] == "ok"
    assert res["tab"] == "alerts"
    assert res["rows"] == 2
    update_calls = [c for c in calls if c[1:4] == ("spreadsheets", "values", "update")]
    assert len(update_calls) == 1
    body = json.loads(update_calls[0][update_calls[0].index("--json") + 1])
    assert body["values"][0] == ["a", "b"]
    assert body["values"][2] == ["2", ""]


def test_push_summary_unknown_table_raises(monkeypatch):
    _mock_cli(monkeypatch, connected=True)
    monkeypatch.setenv("AETHER_SHEETS_SPREADSHEET_ID", "sid123")
    with pytest.raises(ValueError):
        SheetMirror().push_summary("nope", [{"a": 1}])


def test_push_summary_skipped_when_disabled(monkeypatch):
    _mock_cli(monkeypatch, connected=False)
    res = SheetMirror().push_summary("alerts", [{"a": 1}])
    assert res["status"] == "skipped"


def test_health_ok_when_connected(monkeypatch):
    _mock_cli(monkeypatch, connected=True)
    monkeypatch.setenv("AETHER_SHEETS_SPREADSHEET_ID", "sid123")
    h = SheetMirror().health()
    assert h["status"] == "ok"
