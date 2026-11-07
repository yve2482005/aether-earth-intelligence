"""Daily-archive + training workbooks for the AETHER Sheets mirror.

The main workbook (backend/integrations/sheets.py) mirrors every table in
full EXCEPT the two firehoses — weather_forecasts (~570k rows x 29 cols,
+355k rows/day) and earthquake_updates — which outgrow the Google Sheets
hard cap of 10,000,000 cells per workbook. This module gives them (and the
ML training pairs) their own workbooks:

  * AETHER_Data_YYYY-MM-DD — one workbook per completed UTC day holding
    that day's weather_forecasts (one tab per forecast model) and
    earthquake_updates (one tab). If a day's cells would exceed
    CELL_BUDGET, the day is split across AETHER_Data_YYYY-MM-DD,
    AETHER_Data_YYYY-MM-DD_2, ... (model tabs packed greedily). A day is
    archived at most once (day-guard state), and workbooks are located by
    name via Drive search before creation, so a lost state file or an
    interrupted run never duplicates a workbook — tabs are rewritten.

  * AETHER_Training_Data — a snapshot of the FULL training pairs built by
    the trainer's own ml/weather/dataset.py build_dataset() (features +
    per-task targets + valid_time + location_id + regime per row), plus a
    `registry` tab (dataset_version, exported_at, pair rows, period,
    locations, tab layout). The workbook is replaced only when
    dataset_version changes; otherwise the sync reports it skipped. If the
    pairs ever exceed CELL_BUDGET they are packed by location groups
    across AETHER_Training_Data, AETHER_Training_Data_2, ... — pairs are
    never silently truncated; the registry records the full layout.

State lives in bootstrap/SHEETS_ARCHIVE_STATE.json (workbook ids, archived
days, training snapshot marker). A day's tabs are checkpointed in the
state's `in_progress_days` section as each one finishes (see
archive_pending_days), so an interrupted day resumes mid-day instead of
restarting from its first tab. The main-workbook mirror keeps its per-tab
change signatures in the same file's `mirror` section (scripts/sync_sheets).
Transport is the same connected-account `hatch_gws_cli` used by
sheets.py — no service-account keys, zero spend.
"""

from __future__ import annotations

import json
import logging
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from backend.integrations import sheets as sheets_mod

log = logging.getLogger("aether.integrations.sheets_archive")

REPO_ROOT = Path(__file__).resolve().parents[2]
STATE_FILE = REPO_ROOT / "bootstrap" / "SHEETS_ARCHIVE_STATE.json"

# Google Sheets hard cap is 10,000,000 cells per workbook; keep a margin.
CELL_BUDGET = 9_000_000
# Batched writes: at most ~5,000 rows per values.update call, and at most
# ~100 KB of JSON payload. The payload travels as one CLI argument, and
# Linux caps a single argument at MAX_ARG_STRLEN (128 KiB) — a 1 MB chunk
# fails with "Argument list too long" (hit on the first real archive run,
# 2026-10-17). 100 KB leaves headroom for the --params argument.
MAX_CHUNK_ROWS = 5000
MAX_CHUNK_BYTES = 100_000

ARCHIVE_PREFIX = "AETHER_Data"
TRAINING_WORKBOOK = "AETHER_Training_Data"
UPDATES_TAB = "earthquake_updates"

_cli = sheets_mod._cli  # re-exported indirection point for tests


# --------------------------------------------------------------------- state
def load_state() -> Dict[str, Any]:
    try:
        if STATE_FILE.is_file():
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(state, dict):
                # setdefault keeps pre-2026-10-19 state files (which lack
                # in_progress_days / mirror) loading unchanged.
                state.setdefault("workbooks", {})
                state.setdefault("archived_days", {})
                state.setdefault("in_progress_days", {})
                state.setdefault("training", {})
                state.setdefault("mirror", {})
                return state
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("archive state unreadable (%s); rebuilding", exc)
    return {"workbooks": {}, "archived_days": {}, "in_progress_days": {},
            "training": {}, "mirror": {}}


def save_state(state: Dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, STATE_FILE)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ------------------------------------------------------------------ workbook
def find_spreadsheet_by_name(name: str) -> Optional[str]:
    """Drive search for a spreadsheet by exact title; None when absent."""
    q = ("name = '%s' and mimeType = 'application/vnd.google-apps.spreadsheet'"
         " and trashed = false" % name.replace("'", "\\'"))
    try:
        out = _cli(
            "drive", "files", "list",
            "--params", json.dumps({
                "q": q, "fields": "files(id,name)", "pageSize": 5,
            }),
        )
    except RuntimeError as exc:
        log.warning("drive search for %r failed: %s", name, exc)
        return None
    for f in out.get("files", []):
        if f.get("name") == name:
            return f.get("id")
    return None


def ensure_workbook(state: Dict[str, Any], name: str) -> Tuple[str, bool]:
    """Return (spreadsheet_id, created) for `name`, creating it if needed.

    Resolution order: local state -> Drive search by name -> create.
    """
    known = state.get("workbooks", {}).get(name)
    if known:
        return known, False
    found = find_spreadsheet_by_name(name)
    if found:
        state.setdefault("workbooks", {})[name] = found
        return found, False
    out = _cli(
        "sheets", "spreadsheets", "create",
        "--json", json.dumps({"properties": {"title": name}}),
    )
    sid = out.get("spreadsheetId") or out.get("spreadsheet_id")
    if not sid and isinstance(out.get("properties"), dict):
        sid = out["properties"].get("spreadsheetId")
    if not sid:
        raise RuntimeError("spreadsheets.create returned no id for %r" % name)
    state.setdefault("workbooks", {})[name] = sid
    return sid, True


def get_tabs(spreadsheet_id: str) -> Dict[str, int]:
    """{tab title: sheetId} for a workbook."""
    meta = _cli(
        "sheets", "spreadsheets", "get",
        "--params", json.dumps({
            "spreadsheetId": spreadsheet_id,
            "fields": "sheets(properties)",
        }),
    )
    tabs: Dict[str, int] = {}
    for sheet in meta.get("sheets", []):
        props = sheet.get("properties", {})
        if props.get("title") is not None:
            tabs[props["title"]] = props.get("sheetId")
    return tabs


def ensure_tabs(spreadsheet_id: str,
                specs: Dict[str, Tuple[int, int]]) -> Dict[str, str]:
    """Ensure each tab in specs ({title: (rows, cols)}) exists with a grid
    at least that large. Returns {tab: 'created'|'renamed'|'exists'}."""
    existing = get_tabs(spreadsheet_id)
    result: Dict[str, str] = {}
    requests: List[Dict[str, Any]] = []
    missing: List[str] = []
    for title, (rows, cols) in specs.items():
        if title in existing:
            result[title] = "exists"
        else:
            missing.append(title)
    if missing:
        # A freshly created workbook has one default tab ("Sheet1"); rename
        # it to the first needed tab instead of leaving an orphan behind.
        renamed = None
        if "Sheet1" in existing and len(existing) == 1:
            renamed = missing[0]
            rows, cols = specs[renamed]
            requests.append({"updateSheetProperties": {
                "properties": {
                    "sheetId": existing["Sheet1"],
                    "title": renamed,
                    "gridProperties": {
                        "rowCount": max(1000, rows + 101),
                        "columnCount": max(26, cols + 2),
                    },
                },
                "fields": "title,gridProperties",
            }})
            result[renamed] = "renamed"
        for title in missing:
            if title == renamed:
                continue
            rows, cols = specs[title]
            requests.append({"addSheet": {"properties": {
                "title": title,
                "gridProperties": {
                    "rowCount": max(1000, rows + 101),
                    "columnCount": max(26, cols + 2),
                },
            }}})
            result[title] = "created"
    if requests:
        _cli(
            "sheets", "spreadsheets", "batchUpdate",
            "--params", json.dumps({"spreadsheetId": spreadsheet_id}),
            "--json", json.dumps({"requests": requests}),
        )
    return result


def _expand_grid(spreadsheet_id: str, tab: str, rows: int, cols: int) -> None:
    tabs = get_tabs(spreadsheet_id)
    sheet_id = tabs.get(tab)
    if sheet_id is None:
        raise RuntimeError("cannot expand grid: tab %r not found" % tab)
    _cli(
        "sheets", "spreadsheets", "batchUpdate",
        "--params", json.dumps({"spreadsheetId": spreadsheet_id}),
        "--json", json.dumps({"requests": [{"updateSheetProperties": {
            "properties": {"sheetId": sheet_id, "gridProperties": {
                "rowCount": max(3000, rows + 100),
                "columnCount": max(26, cols + 2)}},
            "fields": "gridProperties"}}]}),
    )


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def write_batched(spreadsheet_id: str, tab: str, headers: Sequence[str],
                  rows: Sequence[Sequence[Any]]) -> Dict[str, Any]:
    """Clear `tab` and rewrite it (header + rows) in bounded batches.

    Batches hold at most MAX_CHUNK_ROWS rows and at most ~MAX_CHUNK_BYTES
    of JSON payload, whichever binds first.

    Streaming (changed 2026-10-18): rows are converted and written one
    chunk at a time instead of materializing the full cell matrix (and
    chunk list) up front — a 185k-row archive day previously peaked at
    ~2.3 GB RSS for the whole sync process and starved the collectors'
    page cache. Rows may be sequences (header order) or dicts (looked
    up by header name). The grid-expand retry simply re-iterates.
    """
    ncols = len(headers)
    total = len(rows)

    def _write_all() -> None:
        _cli(
            "sheets", "spreadsheets", "values", "clear",
            "--params", json.dumps({
                "spreadsheetId": spreadsheet_id,
                "range": "%s!A:ZZ" % tab,
            }),
        )
        idx = 0
        first = True
        while first or idx < total:
            chunk: List[List[str]] = [list(headers)] if first else []
            size = len(json.dumps(chunk[0])) if chunk else 0
            budget = MAX_CHUNK_ROWS + (1 if first else 0)
            start_idx = idx
            while idx < total and len(chunk) < budget:
                raw = rows[idx]
                if isinstance(raw, dict):
                    row = [_cell(raw.get(h)) for h in headers]
                else:
                    row = [_cell(v) for v in raw]
                row_json = len(json.dumps(row))
                if chunk and size + row_json > MAX_CHUNK_BYTES:
                    break
                chunk.append(row)
                size += row_json
                idx += 1
            start_row = 1 if first else start_idx + 2
            _write_chunk(spreadsheet_id, tab, chunk, start_row)
            first = False

    try:
        try:
            _write_all()
        except RuntimeError as exc:
            if "exceeds grid limits" not in str(exc):
                raise
            _expand_grid(spreadsheet_id, tab, total + 1, ncols)
            _write_all()
    except RuntimeError as exc:
        return {"status": "error", "tab": tab, "reason": str(exc)[:200],
                "rows": 0}
    return {"status": "ok", "tab": tab, "rows": total}


def _write_chunk(spreadsheet_id: str, tab: str, chunk: List[List[str]],
                 start_row: int) -> None:
    _cli(
        "sheets", "spreadsheets", "values", "update",
        "--params", json.dumps({
            "spreadsheetId": spreadsheet_id,
            "range": "%s!A%d" % (tab, start_row),
            "valueInputOption": "RAW",
        }),
        "--json", json.dumps({"values": chunk}),
    )


def read_values(spreadsheet_id: str, rng: str) -> List[List[str]]:
    out = _cli(
        "sheets", "spreadsheets", "values", "get",
        "--params", json.dumps({"spreadsheetId": spreadsheet_id, "range": rng}),
    )
    return out.get("values", []) or []


# ------------------------------------------------------------- daily archive
def _day_updates(conn, day: str) -> List[Dict[str, Any]]:
    """Earthquake updates for one day (small — a few thousand rows)."""
    from backend.db import db as dbmod
    return dbmod.query(
        conn,
        "SELECT * FROM earthquake_updates WHERE substr(received_at, 1, 10) = ?"
        " ORDER BY rowid",
        (day,),
    )


def _day_model_counts(conn, day: str) -> Dict[str, int]:
    """Forecast row counts per model for one collected_at day (cheap)."""
    rows = conn.execute(
        "SELECT COALESCE(model, 'unknown') AS m, COUNT(*) AS c"
        " FROM weather_forecasts WHERE substr(collected_at, 1, 10) = ?"
        " GROUP BY m",
        (day,),
    ).fetchall()
    return {r["m"]: r["c"] for r in rows}


def _model_day_rows(conn, day: str, model: str) -> List[Dict[str, Any]]:
    """One model's forecast rows for one day — loaded, written, freed.

    Changed 2026-10-18: the old _day_rows fetchall'd the WHOLE day
    (867,230 rows for 2026-10-17 after the scale-up) and peaked the
    archive process at ~2 GB RSS, starving the collectors. Per-model
    loads cap the peak at roughly one model's share (~217k rows).
    """
    from backend.db import db as dbmod
    return dbmod.query(
        conn,
        "SELECT * FROM weather_forecasts"
        " WHERE substr(collected_at, 1, 10) = ?"
        " AND COALESCE(model, 'unknown') = ? ORDER BY rowid",
        (day, model),
    )


def _table_headers(conn, table: str) -> List[str]:
    return [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]


def pending_days(conn, today: Optional[str] = None) -> List[str]:
    """Completed UTC days (< today) present in either firehose table."""
    today = today or _today_utc()
    days = set()
    for table, col in (("weather_forecasts", "collected_at"),
                       ("earthquake_updates", "received_at")):
        try:
            rows = conn.execute(
                "SELECT DISTINCT substr(%s, 1, 10) AS d FROM %s" % (col, table)
            ).fetchall()
        except Exception:  # noqa: BLE001 - missing table -> no days
            continue
        for r in rows:
            d = r[0] if not hasattr(r, "keys") else r["d"]
            if d and d < today:
                days.add(d)
    return sorted(days)


def _pack_day(model_counts: Dict[str, int], f_headers: List[str],
              updates: List[Dict[str, Any]], u_headers: List[str],
              day: str) -> List[Dict[str, Any]]:
    """Plan one day's workbook groups from per-model row COUNTS.

    Returns [{workbook, tabs: {title: spec}, cells}] where a forecast
    spec is {"headers", "model", "count"} (rows are loaded per model at
    write time — never the whole day at once) and the updates spec is
    {"headers", "rows", "count"} (updates are small enough to carry).
    earthquake_updates rides in the first workbook; forecast model tabs
    are packed greedily under CELL_BUDGET.
    """
    if not model_counts and not updates:
        return []

    groups: List[Dict[str, Any]] = []

    def new_group() -> Dict[str, Any]:
        name = "%s_%s" % (ARCHIVE_PREFIX, day)
        if groups:
            name = "%s_%d" % (name, len(groups) + 1)
        g = {"workbook": name, "tabs": {}, "cells": 0}
        groups.append(g)
        return g

    group = new_group()
    if updates:
        group["tabs"][UPDATES_TAB] = {"headers": u_headers, "rows": updates,
                                      "count": len(updates)}
        group["cells"] += (len(updates) + 1) * len(u_headers)
    for model in sorted(model_counts):
        count = model_counts[model]
        cells = (count + 1) * len(f_headers)
        if group["cells"] + cells > CELL_BUDGET and group["tabs"]:
            group = new_group()
        group["tabs"][model] = {"headers": f_headers, "model": model,
                                "count": count}
        group["cells"] += cells
    return groups


def archive_pending_days(conn, state: Optional[Dict[str, Any]] = None,
                         today: Optional[str] = None) -> Dict[str, Any]:
    """Archive every completed, not-yet-archived UTC day. Day-guarded:
    days already in `archived_days` are never rewritten.

    Tab-checkpointed (changed 2026-10-19): a day is no longer all-or-
    nothing. Each tab is recorded in state["in_progress_days"][day]
    {"tabs": {"<workbook>:<tab>": {"rows": n}}} as soon as its write
    succeeds, and the state is saved after EVERY tab. A run killed
    mid-day (the 3300s stage cap used to burn the whole sync on the
    867k-row day 2026-10-17, pass after pass) leaves the finished tabs
    checkpointed; the next run skips a checkpointed tab whose recorded
    row count still matches the count it would push (a mismatch —
    late-arriving rows for that day — re-pushes the tab), and only the
    remaining tabs are written. When every tab of the day is done, the
    day is promoted into `archived_days` with exactly the entry shape
    it has always had, and its in-progress record is cleared.
    """
    if not sheets_mod._cli_connected():
        return {"status": "skipped", "reason": "not_configured", "days": []}
    state = state if state is not None else load_state()
    done = state.setdefault("archived_days", {})
    in_progress = state.setdefault("in_progress_days", {})
    results: List[Dict[str, Any]] = []
    for day in pending_days(conn, today=today):
        if day in done:
            results.append({"day": day, "status": "already_archived"})
            continue
        # Per-day isolation: a failed day (e.g. a Sheets call killed by the
        # hard timeout in sheets._cli during a proxy stall) is recorded and
        # the remaining days still archive; the day stays out of `done`, so
        # the next sync retries it — now from its tab checkpoint.
        try:
            updates = _day_updates(conn, day)
            model_counts = _day_model_counts(conn, day)
            forecast_total = sum(model_counts.values())
            f_headers = (_table_headers(conn, "weather_forecasts")
                         if model_counts else [])
            u_headers = list(updates[0].keys()) if updates else []
            groups = _pack_day(model_counts, f_headers, updates, u_headers,
                               day)
            if not groups:
                done[day] = {"workbooks": [], "forecast_rows": 0,
                             "update_rows": 0, "tabs": [],
                             "archived_at": _utc_now(), "note": "no rows"}
                in_progress.pop(day, None)
                save_state(state)
                results.append({"day": day, "status": "ok", "workbooks": [],
                                "forecast_rows": 0, "update_rows": 0})
                continue
            tabs_done: Dict[str, Any] = in_progress.setdefault(
                day, {"tabs": {}}).setdefault("tabs", {})
            day_tabs: List[str] = []
            day_books: List[str] = []
            created = False
            resumed_tabs = 0
            for group in groups:
                sid, created = ensure_workbook(state, group["workbook"])
                pending: Dict[str, Any] = {}
                for tab, spec in group["tabs"].items():
                    key = "%s:%s" % (group["workbook"], tab)
                    day_tabs.append(key)
                    rec = tabs_done.get(key)
                    if isinstance(rec, dict) \
                            and rec.get("rows") == spec["count"]:
                        resumed_tabs += 1  # checkpoint hit: tab is written
                    else:
                        pending[tab] = spec
                if pending:
                    specs = {t: (spec["count"] + 1, len(spec["headers"]))
                             for t, spec in pending.items()}
                    ensure_tabs(sid, specs)
                    for tab, spec in pending.items():
                        if "rows" in spec:
                            rows = spec["rows"]
                            out = write_batched(sid, tab, spec["headers"],
                                                rows)
                        else:
                            # Load one model at a time; freed before next.
                            rows = _model_day_rows(conn, day, spec["model"])
                            try:
                                out = write_batched(sid, tab,
                                                    spec["headers"], rows)
                            finally:
                                del rows
                        if out["status"] != "ok":
                            raise RuntimeError(
                                "archive write failed for %s/%s: %s"
                                % (group["workbook"], tab,
                                   out.get("reason")))
                        tabs_done["%s:%s" % (group["workbook"], tab)] = \
                            {"rows": spec["count"]}
                        # Checkpoint after EACH tab: a kill during the
                        # next tab loses nothing already written.
                        save_state(state)
                day_books.append(group["workbook"])
                save_state(state)  # workbook ids survive an interrupted day
            done[day] = {
                "workbooks": day_books,
                "forecast_rows": forecast_total,
                "update_rows": len(updates),
                "tabs": day_tabs,
                "archived_at": _utc_now(),
            }
            in_progress.pop(day, None)
            save_state(state)
            entry = {"day": day, "status": "ok", "workbooks": day_books,
                     "forecast_rows": forecast_total,
                     "update_rows": len(updates),
                     "created": created}
            if resumed_tabs:
                entry["resumed_tabs"] = resumed_tabs
            results.append(entry)
        except RuntimeError as exc:
            log.warning("archive failed for %s: %s", day, exc)
            results.append({"day": day, "status": "error",
                            "reason": str(exc)[:200]})
    archived = [r for r in results if r["status"] == "ok"]
    failed = [r for r in results if r["status"] == "error"]
    return {"status": "ok" if not failed else "partial",
            "days": results,
            "days_archived": len(archived),
            "days_failed": len(failed),
            "forecast_rows": sum(r.get("forecast_rows", 0) for r in archived),
            "update_rows": sum(r.get("update_rows", 0) for r in archived)}


# ---------------------------------------------------------- training workbook
def _training_rows(ds) -> Tuple[List[str], List[List[Any]], List[int]]:
    """Flatten a WeatherDataset into export rows (sorted by location, time).

    Returns (headers, rows, location_ids aligned with rows).
    """
    from ml.weather.dataset import REGIMES, TASKS, epoch_to_iso

    order = sorted(range(ds.n_pairs),
                   key=lambda i: (ds.location_ids[i], ds.valid_times[i]))
    headers = (["valid_time", "location_id", "regime", "forecast_hour"]
               + list(ds.feature_names)
               + ["target_%s" % t for t in TASKS])
    rows: List[List[Any]] = []
    locs: List[int] = []
    for i in order:
        row: List[Any] = [epoch_to_iso(int(ds.valid_times[i])),
                          int(ds.location_ids[i]),
                          REGIMES[int(ds.regimes[i])],
                          float(ds.forecast_hours[i])]
        row.extend(float(v) for v in ds.X[i])
        row.extend(float(ds.y[t][i]) for t in TASKS)
        rows.append(row)
        locs.append(int(ds.location_ids[i]))
    return headers, rows, locs


def _pack_training(headers: List[str], rows: List[List[Any]],
                   locs: List[int]) -> List[Dict[str, Any]]:
    """Pack rows (pre-sorted by location) into workbook parts under
    CELL_BUDGET, splitting only at location boundaries. Returns
    [{workbook, rows, locations}] — pairs are never dropped."""
    ncols = len(headers)
    total_cells = (len(rows) + 1) * ncols
    if total_cells <= CELL_BUDGET:
        return [{"workbook": TRAINING_WORKBOOK, "rows": rows,
                 "locations": sorted(set(locs))}]
    parts: List[Dict[str, Any]] = []
    cur_rows: List[List[Any]] = []
    cur_locs: List[int] = []
    idx = 0
    while idx < len(rows):
        loc = locs[idx]
        j = idx
        while j < len(rows) and locs[j] == loc:
            j += 1
        block = rows[idx:j]
        block_cells = len(block) * ncols
        if cur_rows and (len(cur_rows) + len(block) + 1) * ncols > CELL_BUDGET:
            parts.append({"workbook": None, "rows": cur_rows,
                          "locations": sorted(set(cur_locs))})
            cur_rows, cur_locs = [], []
        if block_cells > CELL_BUDGET:
            raise RuntimeError(
                "training pairs for location %s alone exceed the Sheets "
                "cell budget (%d cells); cannot store without splitting a "
                "location group" % (loc, block_cells))
        cur_rows.extend(block)
        cur_locs.append(loc)
        idx = j
    if cur_rows:
        parts.append({"workbook": None, "rows": cur_rows,
                      "locations": sorted(set(cur_locs))})
    for k, part in enumerate(parts):
        part["workbook"] = (TRAINING_WORKBOOK if k == 0
                            else "%s_%d" % (TRAINING_WORKBOOK, k + 1))
    return parts


def _registry_rows(ds, exported_at: str, part: Dict[str, Any],
                   part_no: int, parts_total: int,
                   layout: str) -> List[List[str]]:
    return [
        ["dataset_version", ds.dataset_version],
        ["exported_at", exported_at],
        ["pair_rows_total", str(ds.n_pairs)],
        ["pair_rows_this_workbook", str(len(part["rows"]))],
        ["period_start", ds.time_min],
        ["period_end", ds.time_max],
        ["locations_total", str(len(set(ds.location_ids)))],
        ["locations_this_workbook",
         ",".join(str(x) for x in part["locations"])],
        ["workbook_part", "%d of %d" % (part_no, parts_total)],
        ["tab_layout", layout],
        ["source", "ml/weather/dataset.py build_dataset()"],
    ]


def sync_training_workbook(db_path: Optional[str] = None,
                           state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Snapshot the full training pairs into AETHER_Training_Data.

    Skips the rewrite when dataset_version is unchanged (state marker,
    cross-checked against the workbook's own registry tab when the state
    marker is missing). Never truncates pairs: oversize datasets are
    packed across multiple workbooks by location group.
    """
    if not sheets_mod._cli_connected():
        return {"status": "skipped", "reason": "not_configured"}
    from ml.weather.dataset import build_dataset

    if not db_path:
        from backend.common.config import DB_PATH
        db_path = str(DB_PATH)
    ds = build_dataset(str(db_path))
    state = state if state is not None else load_state()
    marker = state.setdefault("training", {})

    if marker.get("dataset_version") == ds.dataset_version:
        return {"status": "skipped", "reason": "dataset_version unchanged",
                "dataset_version": ds.dataset_version,
                "pair_rows": ds.n_pairs}
    # Recovery: state lost but workbook exists with the same version.
    if not marker.get("dataset_version"):
        sid = find_spreadsheet_by_name(TRAINING_WORKBOOK)
        if sid:
            try:
                values = read_values(sid, "registry!A:B")
                reg = {r[0]: r[1] for r in values if len(r) >= 2}
            except RuntimeError:
                reg = {}
            if reg.get("dataset_version") == ds.dataset_version:
                marker.update({
                    "dataset_version": ds.dataset_version,
                    "spreadsheet_id": sid,
                    "pair_rows": ds.n_pairs,
                    "exported_at": reg.get("exported_at"),
                    "recovered_from_registry": True,
                })
                state.setdefault("workbooks", {})[TRAINING_WORKBOOK] = sid
                save_state(state)
                return {"status": "skipped",
                        "reason": "dataset_version unchanged (registry)",
                        "dataset_version": ds.dataset_version,
                        "pair_rows": ds.n_pairs}

    headers, rows, locs = _training_rows(ds)
    parts = _pack_training(headers, rows, locs)
    exported_at = _utc_now()
    layout = "; ".join(
        "%s: tab 'pairs' (%d rows, locations %s)"
        % (p["workbook"], len(p["rows"]),
           ",".join(str(x) for x in p["locations"]))
        for p in parts)
    written = []
    for k, part in enumerate(parts):
        sid, _created = ensure_workbook(state, part["workbook"])
        ensure_tabs(sid, {"pairs": (len(part["rows"]) + 1, len(headers)),
                          "registry": (12, 2)})
        out = write_batched(sid, "pairs", headers, part["rows"])
        if out["status"] != "ok":
            raise RuntimeError("training pairs write failed for %s: %s"
                               % (part["workbook"], out.get("reason")))
        reg = _registry_rows(ds, exported_at, part, k + 1, len(parts), layout)
        out = write_batched(sid, "registry", ["key", "value"], reg)
        if out["status"] != "ok":
            raise RuntimeError("training registry write failed for %s: %s"
                               % (part["workbook"], out.get("reason")))
        written.append({"workbook": part["workbook"], "spreadsheet_id": sid,
                        "pair_rows": len(part["rows"]),
                        "locations": part["locations"]})
        save_state(state)
    marker.update({
        "dataset_version": ds.dataset_version,
        "exported_at": exported_at,
        "pair_rows": ds.n_pairs,
        "period_start": ds.time_min,
        "period_end": ds.time_max,
        "locations": len(set(ds.location_ids)),
        "workbooks": written,
        "tab_layout": layout,
    })
    save_state(state)
    return {"status": "ok", "dataset_version": ds.dataset_version,
            "pair_rows": ds.n_pairs, "workbooks": written,
            "tab_layout": layout, "exported_at": exported_at}
