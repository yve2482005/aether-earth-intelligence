"""Google Sheets mirror for AETHER Earth Intelligence.

Sheets is a MIRROR only — never the primary database. Primary storage is
always local SQLite (data/aether.db).

Transport: the user's CONNECTED Google account via the vendored
`hatch_gws_cli` (same account the user linked in chat). No service-account
keys are used or needed.

Activation rule: the mirror is ACTIVE when
    AETHER_SHEETS_SPREADSHEET_ID  — target spreadsheet id (env), OR
    bootstrap/SHEETS_ID.txt       — saved spreadsheet id (fallback)
is present AND `hatch_gws_cli sheets status` reports connected.

When unconfigured, SheetMirror is disabled and health() returns
"not_configured"; the rest of the system runs fully on local SQLite.

Layout (full mirror, since 2026-10-17):
  * The main workbook mirrors EVERY database table in full, except the two
    firehoses (weather_forecasts, earthquake_updates), which outgrow the
    10,000,000-cell workbook cap. Each mirrored tab is bounded by a
    per-tab safety guard (ROW_GUARD newest rows; truncation is marked in
    the catalog tab).
  * The firehoses live in per-day archive workbooks (AETHER_Data_YYYY-MM-DD)
    written by backend/integrations/sheets_archive.py, as do the ML
    training pairs (AETHER_Training_Data).
  * A `catalog` tab lists every dataset: DB rows, rows in Sheets, where
    it lives, and when it last synced.

Original control tabs (spec section 26) are preserved as tab names where
they exist (observations, source_health); the legacy `weather_data` and
`forecast_errors` tabs are left untouched in the workbook — never deleted —
but are no longer written (forecasts moved to the daily archive).
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

log = logging.getLogger("aether.integrations.sheets")

REPO_ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP_SHEET_ID = REPO_ROOT / "bootstrap" / "SHEETS_ID.txt"
GWS_CLI = os.environ.get("AETHER_GWS_CLI", "/opt/hatch/bin/hatch_gws_cli")

# Tables that outgrow the Sheets workbook cell cap; they are mirrored into
# per-day archive workbooks instead (backend/integrations/sheets_archive.py).
FIREHOSE_TABLES = ("weather_forecasts", "earthquake_updates")

# Per-tab safety guard for the main workbook: a mirrored table is written
# in full while it fits; beyond this many rows only the newest ROW_GUARD
# rows are written and the catalog marks the tab as truncated.
ROW_GUARD = 45000

CATALOG_TAB = "catalog"

# Full mirror: SQLite table name -> sheet tab name for every user-data
# table EXCEPT the firehoses above. Legacy tab names from spec section 26
# are kept where they exist (observations, source_health).
TAB_MAP: Dict[str, str] = {
    "alert_preferences": "alert_preferences",
    "alert_revisions": "alert_revisions",
    "alert_state_revisions": "alert_state_revisions",
    "alert_thresholds": "alert_thresholds",
    "alerts": "alerts",
    "data_quality": "data_quality",
    "devices": "devices",
    "earth_events": "earth_events",
    "earthquake_events": "earthquake_events",
    "incident_notes": "incident_notes",
    "incidents": "incidents",
    "ingestion_runs": "ingestion_runs",
    "locations": "locations",
    "ml_batch_predictions": "ml_batch_predictions",
    "model_scores": "model_scores",
    "model_versions": "model_versions",
    "notification_deliveries": "notification_deliveries",
    "notification_history": "notification_history",
    "notify_queue": "notify_queue",
    "ocean_forecasts": "ocean_forecasts",
    "perf_home_bundles": "perf_home_bundles",
    "perf_source_schedule": "perf_source_schedule",
    "saved_locations": "saved_locations",
    "source_failover": "source_failover",
    "source_registry": "source_health",
    "system_health": "system_health",
    "training_runs": "training_runs",
    "travel_mode": "travel_mode",
    "tsunami_events": "tsunami_events",
    "tsunami_messages": "tsunami_messages",
    "users": "users",
    "weather_daily": "weather_daily",
    "weather_observations": "observations",
}

REQUIRED_TABS: List[str] = list(TAB_MAP.values()) + [CATALOG_TAB]

SHEET_ID_ENV = "AETHER_SHEETS_SPREADSHEET_ID"

_connected_cache: Optional[bool] = None


# Hard per-call bound for every hatch_gws_cli invocation. The CLI talks to
# Google through this VM's egress proxy, which intermittently stalls
# connections (Oct 17-18: Sheets syncs wedged 27-60 min at ~0 CPU).
# subprocess.run(timeout=...) is NOT a hard bound: it kills only the direct
# child and then waits for pipe EOF, which a pipe-holding descendant can
# postpone indefinitely. _cli therefore runs the CLI in its own process
# group and SIGKILLs the whole group on timeout, so a stalled call fails
# within CLI_TIMEOUT_S (+ a short reap grace) and the sync moves on to the
# next tab. One retry on timeout only (a proxy stall episode may clear);
# non-timeout failures are not retried.
CLI_TIMEOUT_S = 90
CLI_ATTEMPTS = 2
CLI_RETRY_BACKOFF_S = 3.0


class _CliTimeout(RuntimeError):
    """A hatch_gws_cli call exceeded CLI_TIMEOUT_S and was killed."""


def _run_cli_once(args: tuple) -> Dict[str, Any]:
    try:
        proc = subprocess.Popen(
            [GWS_CLI, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except OSError as exc:
        raise RuntimeError("google workspace cli unavailable: %s" % exc) from exc
    try:
        stdout, stderr = proc.communicate(timeout=CLI_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        try:
            proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            pass  # group is dead; pipes abandoned, raise the timeout
        raise _CliTimeout(
            "gws cli timed out after %ss: %s"
            % (CLI_TIMEOUT_S, " ".join(str(a) for a in args[:3])))
    if proc.returncode != 0:
        raise RuntimeError("gws cli failed: %s" % (stderr or stdout)[:300])
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("gws cli returned non-JSON output") from exc


def _cli(*args: str) -> Dict[str, Any]:
    """Run hatch_gws_cli and return parsed JSON. Raises RuntimeError on failure.

    Every call is hard-bounded by CLI_TIMEOUT_S (process-group kill on
    expiry); a timeout is retried once after CLI_RETRY_BACKOFF_S.
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            return _run_cli_once(args)
        except _CliTimeout:
            if attempt >= CLI_ATTEMPTS:
                raise
            log.warning("gws cli timed out after %ss (attempt %d/%d); "
                        "retrying: %s", CLI_TIMEOUT_S, attempt, CLI_ATTEMPTS,
                        " ".join(str(a) for a in args[:3]))
            time.sleep(CLI_RETRY_BACKOFF_S)


def _cli_connected() -> bool:
    global _connected_cache
    if _connected_cache is not None:
        return _connected_cache
    try:
        status = _cli("sheets", "status")
        _connected_cache = status.get("status") == "connected"
    except RuntimeError as exc:
        log.warning("Sheets status check failed: %s", exc)
        _connected_cache = False
    return _connected_cache


def resolve_spreadsheet_id() -> Optional[str]:
    sid = os.environ.get(SHEET_ID_ENV)
    if sid:
        return sid.strip()
    try:
        if BOOTSTRAP_SHEET_ID.is_file():
            return BOOTSTRAP_SHEET_ID.read_text(encoding="utf-8").strip() or None
    except OSError:
        pass
    return None


class SheetMirror:
    """Control/metadata mirror into Google Sheets. Disabled unless configured."""

    def __init__(self) -> None:
        self._spreadsheet_id: Optional[str] = resolve_spreadsheet_id()
        self._last_error: Optional[str] = None

    # ------------------------------------------------------------------ setup
    @property
    def enabled(self) -> bool:
        return bool(self._spreadsheet_id) and _cli_connected()

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    @property
    def spreadsheet_id(self) -> Optional[str]:
        return self._spreadsheet_id

    # ------------------------------------------------------------------ tabs
    def ensure_tabs(self) -> Dict[str, str]:
        """Create the control tabs if missing. Returns {tab: 'created'|'exists'}."""
        if not self.enabled:
            return {tab: "skipped_not_configured" for tab in REQUIRED_TABS}
        result: Dict[str, str] = {}
        try:
            got = _cli(
                "sheets", "spreadsheets", "get",
                "--params", json.dumps({
                    "spreadsheetId": self._spreadsheet_id,
                    "fields": "sheets(properties(title))",
                }),
            )
            existing = {s["properties"]["title"] for s in got.get("sheets", [])}
        except RuntimeError as exc:
            self._last_error = str(exc)
            return {tab: "error" for tab in REQUIRED_TABS}
        missing = [t for t in REQUIRED_TABS if t not in existing]
        if missing:
            try:
                _cli(
                    "sheets", "spreadsheets", "batchUpdate",
                    "--params", json.dumps({"spreadsheetId": self._spreadsheet_id}),
                    "--json", json.dumps({
                        "requests": [
                            {"addSheet": {"properties": {"title": t}}}
                            for t in missing
                        ]
                    }),
                )
            except RuntimeError as exc:
                self._last_error = str(exc)
                return {tab: "error" for tab in REQUIRED_TABS}
        for tab in REQUIRED_TABS:
            result[tab] = "created" if tab in missing else "exists"
        return result

    # ------------------------------------------------------------------ push
    def push_summary(self, table_name: str, rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
        """Write a batch of rows into the main workbook.

        table_name is mapped through TAB_MAP; unknown names raise ValueError.
        rows: list of dicts. The tab is cleared and rewritten. Returns stats.
        """
        if not self.enabled:
            return {"status": "skipped", "reason": "not_configured", "rows": 0}
        tab = TAB_MAP.get(table_name)
        if tab is None:
            raise ValueError(
                "Unknown table %r; expected one of %s" % (table_name, sorted(TAB_MAP))
            )
        return self.push_tab(tab, rows)

    def push_tab(self, tab: str, rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
        """Clear and rewrite an arbitrary tab of the main workbook."""
        if not self.enabled:
            return {"status": "skipped", "reason": "not_configured", "rows": 0}
        rows = list(rows)
        if not rows:
            return {"status": "ok", "tab": tab, "rows": 0, "seconds": 0.0}
        headers = list(rows[0].keys())
        # Streaming (2026-10-18): cell conversion happens per chunk in
        # _write_all below, not as one full matrix up front (RSS peak).
        started = time.time()

        def _write_all() -> None:
            _cli(
                "sheets", "spreadsheets", "values", "clear",
                "--params", json.dumps({
                    "spreadsheetId": self._spreadsheet_id,
                    "range": "%s!A:Z" % tab,
                }),
            )
            # Chunked writes: a full tab as one CLI argument exceeds the OS
            # argument-size limit, so write in bounded batches. Byte-aware
            # chunking (changed 2026-10-18): the old fixed 200-row chunks
            # needed ~550 CLI calls per pass for ~110k mirrored rows and
            # the sync outgrew its stage cap as tables grew. Use the same
            # budget as the archive writer (<=5000 rows, <=~100 KB JSON
            # per call); wide rows simply produce smaller chunks.
            # Write each chunk as soon as it is full (streaming — the
            # converted cells for the whole tab are never held at once).
            start_row = 1
            chunk: list = [headers]
            chunk_bytes = len(json.dumps(headers))

            def _flush(ch: list, at_row: int) -> None:
                _cli(
                    "sheets", "spreadsheets", "values", "update",
                    "--params", json.dumps({
                        "spreadsheetId": self._spreadsheet_id,
                        "range": "%s!A%d" % (tab, at_row),
                        "valueInputOption": "RAW",
                    }),
                    "--json", json.dumps({"values": ch}),
                )

            for row in rows:
                cells = [self._cell(row.get(h)) for h in headers]
                row_bytes = len(json.dumps(cells)) + 1
                if len(chunk) > 1 and (len(chunk) >= 5001
                                      or chunk_bytes + row_bytes > 100_000):
                    _flush(chunk, start_row)
                    start_row += len(chunk)
                    chunk = []
                    chunk_bytes = 0
                chunk.append(cells)
                chunk_bytes += row_bytes
            if chunk:
                _flush(chunk, start_row)

        try:
            try:
                _write_all()
            except RuntimeError as exc:
                # A tab created with the default 1000-row grid rejects writes
                # past it once the mirrored table grows (observations hit this
                # at 1,014 rows on 2026-10-17 after the worldwide expansion).
                # Enlarge the grid once and retry; anything else re-raises.
                if "exceeds grid limits" not in str(exc):
                    raise
                self._expand_grid(tab, len(rows) + 1)
                _write_all()
        except RuntimeError as exc:
            self._last_error = str(exc)
            return {"status": "error", "reason": str(exc)[:200], "rows": 0}
        elapsed = time.time() - started
        return {"status": "ok", "tab": tab, "rows": len(rows), "seconds": round(elapsed, 3)}

    def write_headers(self, tab: str, headers: List[str]) -> Dict[str, Any]:
        """Clear a tab and write only its header row (empty-table mirror)."""
        if not self.enabled:
            return {"status": "skipped", "reason": "not_configured", "rows": 0}
        started = time.time()
        try:
            _cli(
                "sheets", "spreadsheets", "values", "clear",
                "--params", json.dumps({
                    "spreadsheetId": self._spreadsheet_id,
                    "range": "%s!A:Z" % tab,
                }),
            )
            _cli(
                "sheets", "spreadsheets", "values", "update",
                "--params", json.dumps({
                    "spreadsheetId": self._spreadsheet_id,
                    "range": "%s!A1" % tab,
                    "valueInputOption": "RAW",
                }),
                "--json", json.dumps({"values": [list(headers)]}),
            )
        except RuntimeError as exc:
            self._last_error = str(exc)
            return {"status": "error", "reason": str(exc)[:200], "rows": 0}
        return {"status": "ok", "tab": tab, "rows": 0,
                "seconds": round(time.time() - started, 3)}

    def _expand_grid(self, tab: str, needed_rows: int) -> None:
        """Grow a tab's grid to at least `needed_rows` rows (min 3000)."""
        meta = _cli(
            "sheets", "spreadsheets", "get",
            "--params", json.dumps({
                "spreadsheetId": self._spreadsheet_id,
                "fields": "sheets(properties)",
            }),
        )
        sheet_id = None
        for sheet in meta.get("sheets", []):
            props = sheet.get("properties", {})
            if props.get("title") == tab:
                sheet_id = props.get("sheetId")
                break
        if sheet_id is None:
            raise RuntimeError("cannot expand grid: tab %r not found" % tab)
        _cli(
            "sheets", "spreadsheets", "batchUpdate",
            "--params", json.dumps({"spreadsheetId": self._spreadsheet_id}),
            "--json", json.dumps({"requests": [{"updateSheetProperties": {
                "properties": {"sheetId": sheet_id, "gridProperties": {
                    "rowCount": max(3000, needed_rows + 100)}},
                "fields": "gridProperties.rowCount"}}]}),
        )

    @staticmethod
    def _cell(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
        return str(value)

    # ------------------------------------------------------------------ health
    def health(self) -> Dict[str, Any]:
        """Return integration health: not_configured | ok | down."""
        if not self._spreadsheet_id:
            return {
                "component": "sheets",
                "status": "not_configured",
                "message": "Google Sheets integration not configured; set %s "
                           "or save the id to bootstrap/SHEETS_ID.txt." % SHEET_ID_ENV,
            }
        if not _cli_connected():
            return {
                "component": "sheets",
                "status": "not_configured",
                "message": "Google Sheets is not connected; link the account in chat.",
            }
        try:
            _cli(
                "sheets", "spreadsheets", "get",
                "--params", json.dumps({
                    "spreadsheetId": self._spreadsheet_id,
                    "fields": "properties(title)",
                }),
            )
            return {"component": "sheets", "status": "ok", "message": "spreadsheet reachable"}
        except RuntimeError as exc:
            return {
                "component": "sheets",
                "status": "down",
                "message": "spreadsheet unreachable: %s" % str(exc)[:200],
            }
