"""Advanced collector orchestration layer.

Wraps the EXISTING collectors (imported, never rewritten) with four
capabilities scripts/run_collect.py does not have:

1. Parallel location fan-out — ThreadPoolExecutor (default 4 workers),
   one SQLite connection per worker thread (the DB runs in WAL mode with
   a 30 s busy timeout, so concurrent writers are safe). Per-location
   behaviour is exactly run_collect.collect_location(): Yangon (primary)
   full tier every run; every other location hourly via the same
   hour-guard (has_current_forecast_batch), same forecast horizons.
2. Ingest validation gate — a connection proxy that intercepts
   INSERTs into weather_forecasts and range-checks the bound values
   (temperature -90..60, humidity 0..100, pressure 800..1100,
   wind_speed >= 0, precipitation >= 0). A row failing the gate is never
   stored: the proxy returns a zero-rowcount cursor, so the collector
   counts it as a duplicate while this layer counts it as a REJECT.
   (The collectors' own validate_forecast_row only flags out-of-range
   values and still stores them; this gate is the rejecting layer.
   NULL values are not range violations and pass, as before.)
3. Gap detection + backfill — per location+model, hourly valid_times
   missing from weather_forecasts over the past 48 h are found; the
   location is then re-fetched directly (bypassing the hour-guard,
   which exists to save API quota, not to block repairs) and gaps are
   re-counted. Honest limit: the Open-Meteo forecast fetch starts at
   the current day 00:00 UTC, so only gaps at/after that floor can be
   filled by a re-fetch; older gaps (e.g. hours before a location was
   seeded) are reported as remaining, never papered over.
4. Run ledger — one row per run in ingestion_runs (component
   'advanced_collect') plus one child row per location / global feed /
   backfill (run_id '<run_id>/<name>', component
   'advanced_collect/<kind>'), each with status, rows_written, error,
   and started/ended timestamps (duration is ended - started).

Global feeds (satellite, radar, USGS, regional networks, tsunami
sources/observations, aftershock outlook, alert rebuild) run once,
sequentially, on the main connection — isolated via run_collect's
run_step, exactly as run_collect does, so one failure never crashes
the others.
"""
from __future__ import annotations

import importlib.util
import json
import math
import re
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.alerting import engine as alert_engine
from backend.common.config import DEFAULT_LAT, DEFAULT_LON, DEFAULT_LOCATION_NAME
from backend.common.http import make_session
from backend.common.logging_setup import setup_logging
from backend.common.timeutil import hour_floor, now_utc, utc_iso
from backend.collectors.earthquake import aftershock, usgs
from backend.collectors.earthquake import regional_networks as eq_regional
from backend.collectors.tsunami import warning_sources as tsunami_src
from backend.collectors.tsunami import observations as tsunami_obs
from backend.collectors.weather import openmeteo, radar, satellite
from backend.db import db as dbmod

log = setup_logging("aether.collectors.advanced")

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_run_collect():
    """Load scripts/run_collect.py as a module and reuse its functions.

    Reusing collect_location / collectable_locations /
    has_current_forecast_batch / get_or_create_location / run_step /
    counts guarantees the advanced layer can never drift from the
    canonical per-location behaviour (tiers, hour-guard, horizons).
    """
    spec = importlib.util.spec_from_file_location(
        "aether_run_collect", REPO_ROOT / "scripts" / "run_collect.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_rc = _load_run_collect()

WORLD_FORECAST_DAYS = _rc.WORLD_FORECAST_DAYS
MODELS = openmeteo.MODELS

# ---------------------------------------------------------------------------
# Ingest validation gate
# ---------------------------------------------------------------------------

# field -> (min, max); None bound means unbounded on that side.
VALIDATION_RANGES: Dict[str, tuple] = {
    "temperature": (-90.0, 60.0),
    "humidity": (0.0, 100.0),
    "pressure": (800.0, 1100.0),
    "wind_speed": (0.0, None),
    "precipitation": (0.0, None),
}

_FORECAST_INSERT_RE = re.compile(
    r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+weather_forecasts\s*\(([^)]*)\)",
    re.IGNORECASE)


def value_in_range(field: str, value: Any) -> bool:
    """True when `value` passes the ingest gate for `field`.

    NULL passes (absence is not a range violation — the collectors keep
    missing values as NULL by design). Non-numeric and NaN values fail.
    """
    if value is None:
        return True
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False
    if math.isnan(v):
        return False
    lo, hi = VALIDATION_RANGES[field]
    if lo is not None and v < lo:
        return False
    if hi is not None and v > hi:
        return False
    return True


def check_forecast_params(columns: List[str], params: tuple) -> List[str]:
    """Return the list of gated fields whose bound value is out of range."""
    bad: List[str] = []
    by_col = dict(zip(columns, params))
    for field in VALIDATION_RANGES:
        if field in by_col and not value_in_range(field, by_col[field]):
            bad.append(field)
    return bad


class _RejectedCursor:
    """Zero-row cursor returned for a rejected insert (looks like a dupe)."""
    rowcount = 0
    lastrowid = None
    description = None

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def __iter__(self):
        return iter(())


# Process-wide write serialization (2026-10-18, structural lock-storm
# fix): kernel evidence (/proc/locks) showed the storm's write-lock
# holder is the collector ITSELF — its parallel workers queue on the
# single SQLite write lock in lockstep, each batch stretching to the
# 120 s busy_timeout under this VM's disk load. Fetches stay parallel;
# WRITES through worker connections now serialize on this mutex from a
# transaction's first write until its commit/rollback, so at most one
# worker batch is ever inside SQLite at a time and batches run at raw
# disk speed instead of queueing behind each other.
_WRITE_LOCK = threading.Lock()
_WRITE_OPS = ("INSERT", "UPDATE", "DELETE", "REPLAC")


def _is_write_sql(sql) -> bool:
    return str(sql).lstrip()[:6].upper().startswith(_WRITE_OPS)


class ValidatingConnection:
    """sqlite3 connection proxy enforcing the ingest gate on forecast rows.

    INSERTs into weather_forecasts are column-mapped and range-checked;
    a failing row is counted (self.rejected / self.rejected_by_field)
    and never reaches SQLite. Everything else delegates untouched.
    """

    def __init__(self, conn: sqlite3.Connection):
        object.__setattr__(self, "_conn", conn)
        object.__setattr__(self, "rejected", 0)
        object.__setattr__(self, "rejected_by_field", {})
        object.__setattr__(self, "_holds_write_lock", False)

    # -- write serialization -------------------------------------------
    def _acquire_write_lock(self) -> None:
        if self._holds_write_lock:
            return
        if _WRITE_LOCK.acquire(timeout=600):
            object.__setattr__(self, "_holds_write_lock", True)
        else:  # degraded rather than deadlocked forever
            log.warning("write-lock acquire timed out; proceeding "
                        "unserialized")

    def release_write_lock(self) -> None:
        if self._holds_write_lock:
            object.__setattr__(self, "_holds_write_lock", False)
            try:
                _WRITE_LOCK.release()
            except RuntimeError:
                pass

    def commit(self):
        try:
            return self._conn.commit()
        finally:
            self.release_write_lock()

    def rollback(self):
        try:
            return self._conn.rollback()
        finally:
            self.release_write_lock()

    def close(self):
        self.release_write_lock()
        return self._conn.close()

    def _gate(self, sql: str, params: Optional[tuple]) -> Optional[List[str]]:
        """Bad-field list if this is a gated forecast insert, else None."""
        if not params:
            return None
        m = _FORECAST_INSERT_RE.search(sql or "")
        if not m:
            return None
        columns = [c.strip().strip('"`[]') for c in m.group(1).split(",")]
        if len(columns) != len(params):
            return None  # cannot map safely -> let SQLite handle it
        return check_forecast_params(columns, tuple(params))

    def _record_reject(self, bad_fields: List[str]) -> None:
        self.rejected += 1
        for f in bad_fields:
            self.rejected_by_field[f] = self.rejected_by_field.get(f, 0) + 1

    def execute(self, sql, params=()):
        bad = self._gate(sql, tuple(params) if params else ())
        if bad:
            self._record_reject(bad)
            return _RejectedCursor()
        if _is_write_sql(sql):
            self._acquire_write_lock()
        return self._conn.execute(sql, params)

    def executemany(self, sql, rows):
        rows = list(rows)
        m = _FORECAST_INSERT_RE.search(sql or "")
        if not m:
            if rows and _is_write_sql(sql):
                self._acquire_write_lock()
            return self._conn.executemany(sql, rows)
        columns = [c.strip().strip('"`[]') for c in m.group(1).split(",")]
        kept = []
        for r in rows:
            if len(columns) == len(r):
                bad = check_forecast_params(columns, tuple(r))
                if bad:
                    self._record_reject(bad)
                    continue
            kept.append(r)
        if kept:
            self._acquire_write_lock()
            return self._conn.executemany(sql, kept)
        return _RejectedCursor()

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_conn"), name)

    def __setattr__(self, name, value):
        if name in ("_conn", "rejected", "rejected_by_field"):
            object.__setattr__(self, name, value)
        else:
            setattr(object.__getattribute__(self, "_conn"), name, value)


# ---------------------------------------------------------------------------
# Gap detection + backfill
# ---------------------------------------------------------------------------

def expected_hours(start: datetime, end: datetime) -> List[str]:
    """Hourly ISO valid_times in [start, end) — both hour-aligned UTC."""
    out = []
    t = start
    while t < end:
        out.append(utc_iso(t))
        t += timedelta(hours=1)
    return out


def detect_gaps(conn: sqlite3.Connection, location_id: int, model: str,
                window_hours: int = 48,
                now: Optional[datetime] = None) -> List[str]:
    """Missing hourly valid_times for one location+model over the window.

    The window start is clamped to the first valid_time ever stored for
    this location+model: hours before collection existed for the pair
    are not gaps (a fixed 48h window otherwise over-reports them for
    newly added locations — found by the 2026-10-17 existing-data audit,
    which scanned each pair's full stored range and found 0 missing).
    """
    now = now or now_utc()
    end = hour_floor(now)
    start = end - timedelta(hours=window_hours)
    first = conn.execute(
        "SELECT MIN(valid_time) FROM weather_forecasts"
        " WHERE location_id=? AND model=?",
        (location_id, model)).fetchone()[0]
    if first is None:
        return []
    first_dt = datetime.fromisoformat(first.replace("Z", "+00:00"))
    if first_dt > start:
        start = hour_floor(first_dt)
    present = {r[0] for r in conn.execute(
        "SELECT DISTINCT valid_time FROM weather_forecasts"
        " WHERE location_id=? AND model=? AND valid_time>=? AND valid_time<?",
        (location_id, model, utc_iso(start), utc_iso(end)))}
    return [h for h in expected_hours(start, end) if h not in present]


def detect_all_gaps(conn: sqlite3.Connection, locations: List[Dict[str, Any]],
                    window_hours: int = 48,
                    now: Optional[datetime] = None) -> Dict[str, Dict[str, List[str]]]:
    """{location_name: {model: [missing valid_times]}} — only gaps kept."""
    out: Dict[str, Dict[str, List[str]]] = {}
    for loc in locations:
        per_model = {}
        for model in MODELS:
            missing = detect_gaps(conn, loc["id"], model, window_hours, now)
            if missing:
                per_model[model] = missing
        if per_model:
            out[loc["name"]] = per_model
    return out


# ---------------------------------------------------------------------------
# Run ledger (ingestion_runs)
# ---------------------------------------------------------------------------

def _ledger_insert(conn, run_id: str, component: str, started_at: str,
                   status: str = "running") -> None:
    conn.execute(
        """INSERT INTO ingestion_runs
           (run_id, component, started_at, ended_at, status, rows_written,
            error, created_at)
           VALUES (?,?,?,NULL,?,0,NULL,?)""",
        (run_id, component, started_at, status, started_at))
    conn.commit()


def _ledger_finish(conn, run_id: str, status: str, rows_written: int,
                   error: Optional[str], ended_at: str) -> None:
    conn.execute(
        """UPDATE ingestion_runs SET ended_at=?, status=?, rows_written=?,
                  error=? WHERE run_id=?""",
        (ended_at, status, rows_written, error, run_id))
    conn.commit()


def _ledger_child(conn, parent_run_id: str, kind: str, name: str,
                  started_at: str, ended_at: str, status: str,
                  rows_written: int, error: Optional[str]) -> None:
    _ledger_insert(conn, f"{parent_run_id}/{name}",
                   f"advanced_collect/{kind}", started_at, status=status)
    _ledger_finish(conn, f"{parent_run_id}/{name}", status, rows_written,
                   error, ended_at)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _location_rowcount(conn, location_id: int) -> int:
    # Forecasts only (2026-10-18): the sentinel showed write batches
    # queueing 15-20 s behind each other under IO saturation; the
    # before/after counts ran 4 indexed scans per location on the
    # worker connections and added pure read load. Observations add
    # ~1 row per location-run, so the ledger delta now undercounts
    # by that much — acceptable telemetry tradeoff.
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM weather_forecasts WHERE location_id=?",
            (location_id,)).fetchone()[0]
    except sqlite3.Error:
        return 0


def _step_failures(result: Dict[str, Any]) -> List[str]:
    """Names of failed steps inside a collect_location() result dict."""
    return [k for k, v in result.items()
            if isinstance(v, dict) and v.get("ok") is False]


def _collect_one_location(db_path: Optional[str], loc: Dict[str, Any],
                          primary_id: int) -> Dict[str, Any]:
    """Worker: collect one location on its own gated connection."""
    started_at = utc_iso()
    t0 = time.monotonic()
    raw = dbmod.connect(db_path, busy_timeout_ms=120000)
    conn = ValidatingConnection(raw)
    try:
        session = make_session()
        before = _location_rowcount(raw, loc["id"])
        result = _rc.collect_location(conn, loc, session,
                                      primary=(loc["id"] == primary_id))
        after = _location_rowcount(raw, loc["id"])
        if "skipped" in result:
            status = "skipped"
            failed_steps: List[str] = []
        else:
            failed_steps = _step_failures(result)
            status = "partial" if failed_steps else "ok"
        return {
            "name": loc["name"], "location_id": loc["id"],
            "status": status, "result": result,
            "failed_steps": failed_steps,
            "rows_written": max(after - before, 0),
            "validation_rejected": conn.rejected,
            "validation_rejected_by_field": dict(conn.rejected_by_field),
            "started_at": started_at, "ended_at": utc_iso(),
            "duration_s": round(time.monotonic() - t0, 3),
        }
    finally:
        conn.release_write_lock()
        raw.close()


def _run_global_feeds(conn) -> Dict[str, Any]:
    """Global feeds, once, isolated — the same set/order as run_collect."""
    session = make_session()
    steps = [
        ("satellite", lambda: satellite.collect(conn)),
        ("radar", lambda: radar.collect(conn)),
        ("usgs", lambda: usgs.collect(conn, session=session)),
        ("regional_networks", lambda: eq_regional.collect(conn)),
        ("tsunami_warnings", lambda: tsunami_src.collect(conn, session=session)),
        ("tsunami_observations", lambda: tsunami_obs.collect(conn)),
        ("aftershock", lambda: aftershock.get_aftershock_forecast(conn, session=session)),
        ("alerts", lambda: alert_engine.build_alerts(conn)),
    ]
    out: Dict[str, Any] = {}
    for name, fn in steps:
        started_at = utc_iso()
        t0 = time.monotonic()
        res = _rc.run_step(name, fn, conn=conn)
        out[name] = {**res, "started_at": started_at, "ended_at": utc_iso(),
                     "duration_s": round(time.monotonic() - t0, 3)}
    return out


def _backfill_location(db_path: Optional[str], loc: Dict[str, Any],
                       primary_id: int) -> Dict[str, Any]:
    """Re-fetch one location directly (hour-guard bypassed for repairs)."""
    raw = dbmod.connect(db_path, busy_timeout_ms=120000)
    conn = ValidatingConnection(raw)
    try:
        days = 7 if loc["id"] == primary_id else WORLD_FORECAST_DAYS
        res = openmeteo.collect(conn, loc["id"], loc["latitude"],
                                loc["longitude"], session=make_session(),
                                forecast_days=days)
        models = res.get("models", {}) if isinstance(res, dict) else {}
        failed = [m for m, r in models.items()
                  if isinstance(r, dict) and r.get("ok") is False]
        return {"status": "partial" if failed else "ok",
                "failed_models": failed,
                "validation_rejected": conn.rejected,
                "validation_rejected_by_field": dict(conn.rejected_by_field)}
    finally:
        raw.close()


def _select_fanout_window(locations, primary_id, rotation, cap,
                          tier_b_share=20):
    """Pick this run's fan-out window with a guaranteed Tier B share.

    Bug fixed 2026-11-06: the old ordering (primary, hourly tier,
    least-recently-submitted) filled the whole 80-slot window with Tier A
    every run (253 Tier A > 80), so Tier B was NEVER submitted — its
    forecasts went stale on 2026-10-18 and it collected zero observations.
    The rotation file proved it: 253 Tier A ids, 0 Tier B ids.

    Split the window: cap-tier_b_share slots for Tier A (primary first),
    tier_b_share for Tier B, each ordered least-recently-submitted. A tier
    short of its share yields the remainder to the other tier.
    Returns (window, updated_rotation_dict).
    """
    def _a_key(loc):
        return (0 if loc["id"] == primary_id else 1,
                float(rotation.get(str(loc["id"]), 0.0)))

    tier_a = [l for l in locations
              if int(l.get("collect_every_hours") or 1) <= 1]
    tier_b = [l for l in locations
              if int(l.get("collect_every_hours") or 1) > 1]
    tier_a.sort(key=_a_key)
    tier_b.sort(key=lambda l: float(rotation.get(str(l["id"]), 0.0)))
    n_b = min(tier_b_share, len(tier_b))
    n_a = min(cap - n_b, len(tier_a))
    if n_a < cap - n_b:  # Tier A short: give unused slots to Tier B.
        n_b = min(len(tier_b), cap - n_a)
    if n_b < tier_b_share:  # Tier B short: give unused slots to Tier A.
        n_a = min(len(tier_a), cap - n_b)
    window = tier_a[:n_a] + tier_b[:n_b]
    now_ts = time.time()
    new_rotation = dict(rotation)
    for loc in window:
        new_rotation[str(loc["id"])] = now_ts
    return window, new_rotation


def run_advanced_collection(db_path: Optional[str] = None,
                            max_workers: int = 4,
                            gap_window_hours: int = 48,
                            backfill: bool = True,
                            max_locations_per_run: int = 80) -> Dict[str, Any]:
    """Run the full advanced collection. Returns the JSON summary dict.

    Hard failures (entries in summary["hard_failures"]) are orchestration
    crashes: a location task that raised, or the global/gap phase dying.
    Per-source failures isolated by the collectors themselves are soft:
    they appear in summary["source_failures"] and in the ledger.
    """
    run_id = f"adv-{now_utc().strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    started_at = utc_iso()
    t_run = time.monotonic()
    dbmod.init_db(db_path)
    conn = dbmod.connect(db_path, busy_timeout_ms=120000)
    _ledger_insert(conn, run_id, "advanced_collect", started_at)

    hard_failures: List[str] = []
    source_failures: List[str] = []
    summary: Dict[str, Any] = {
        "run_id": run_id, "started_at": started_at, "max_workers": max_workers,
        "gap_window_hours": gap_window_hours,
    }
    try:
        before = _rc.counts(conn)
        primary = _rc.get_or_create_location(
            conn, DEFAULT_LOCATION_NAME, DEFAULT_LAT, DEFAULT_LON)
        locations = _rc.collectable_locations(conn)
        if not any(l["id"] == primary["id"] for l in locations):
            locations.insert(0, primary)
        primary_id = primary["id"]

        # ---- 1. global feeds FIRST (earthquakes/tsunami/alerts) ---------
        # Reordered 2026-10-17: at ~1,000 locations the location phase can
        # wedge on proxy stalls or hit the cron timeout cap; running the
        # fast global stage first guarantees seismic/tsunami freshness is
        # restored at the start of every run instead of starving at the end.
        g_started, g_t0 = utc_iso(), time.monotonic()
        try:
            globals_out = _run_global_feeds(conn)
        except Exception as e:  # noqa: BLE001 - run_step isolates steps;
            hard_failures.append("global_feeds")  # reaching here = phase crash
            globals_out = {}
            log.exception("global feeds phase crashed: %s", e)
        for name, res in globals_out.items():
            if not res.get("ok"):
                source_failures.append(f"global:{name}")
            _ledger_child(conn, run_id, "global", f"global:{name}",
                          res["started_at"], res["ended_at"],
                          "ok" if res.get("ok") else "failed", 0,
                          res.get("error"))
        summary["global_feeds"] = {
            n: {"ok": r.get("ok", False), "duration_s": r["duration_s"],
                **({"error": r["error"]} if not r.get("ok") else {})}
            for n, r in globals_out.items()}

        # ---- 1a. transaction hygiene --------------------------------------
        # The main connection must not carry an open transaction into
        # the fan-out: it would hold the SQLite write lock for the whole
        # sweep while the main thread waits on futures (the holderless
        # storm signature — see run_collect.run_step's invariant).
        if conn.in_transaction:
            log.warning("main connection held an open transaction after "
                        "the global feeds; committing before fan-out")
            conn.commit()

        # ---- 1b. index warm-up -------------------------------------------
        # Every collector run is a fresh process on a slow disk: the
        # forecast indexes (~141 MB) start cold, and the first worker
        # inserts fault them in page by page while the other workers
        # queue on the write lock — the seed of the recurring "database
        # is locked" storms (stack dumps show all workers parked inside
        # executemany; isolated benchmarks on warm copies take 0.14 s).
        # One sequential scan per index on this connection pulls the
        # pages into the OS page cache before the fan-out starts.
        _warm_t0 = time.monotonic()
        try:
            for _idx in ("sqlite_autoindex_weather_forecasts_1",
                         "idx_wf_loc_time"):
                conn.execute(
                    "SELECT COUNT(*) FROM weather_forecasts INDEXED BY %s"
                    % _idx).fetchone()
        except Exception as exc:  # noqa: BLE001 - warm-up is best-effort
            log.warning("index warm-up failed: %s", exc)
        summary["index_warm_s"] = round(time.monotonic() - _warm_t0, 2)

        # ---- 1c. fan-out window (rotation, 2026-10-18) -------------------
        # Measured reality on this VM: a run collects healthily for its
        # first minutes (~60 locations), then write contention degrades
        # it and the deterministic submission order starves the tail of
        # the list every run. So each run now submits a bounded window
        # ordered by (primary, hourly tier, least-recently-submitted) —
        # the window FITS the healthy span, and the rotation state file
        # guarantees every location cycles through across runs. The
        # in-worker freshness guard still applies inside the window.
        try:
            from backend.common.config import DATA_DIR as _DD

            _rot_path = _DD / "fanout_rotation.json"
            try:
                _rotation = json.loads(_rot_path.read_text())
                if not isinstance(_rotation, dict):
                    _rotation = {}
            except Exception:  # noqa: BLE001 - missing/corrupt = fresh
                _rotation = {}

            _window, _rotation = _select_fanout_window(
                locations, primary_id, _rotation, max_locations_per_run)
            try:
                _tmp = _rot_path.with_suffix(".tmp")
                _tmp.write_text(json.dumps(_rotation))
                _tmp.replace(_rot_path)
            except Exception:  # noqa: BLE001 - rotation is best-effort
                pass
            summary["fanout_window"] = {
                "submitted": len(_window),
                "total_locations": len(locations),
                "cap": max_locations_per_run,
                "tier_a": sum(1 for l in _window
                              if int(l.get("collect_every_hours") or 1) <= 1),
                "tier_b": sum(1 for l in _window
                              if int(l.get("collect_every_hours") or 1) > 1),
            }
            locations = _window
        except Exception:  # noqa: BLE001 - windowing must never kill a run
            log.exception("fan-out windowing failed; submitting all")

        # ---- 2. parallel location fan-out -------------------------------
        loc_results: Dict[str, Any] = {}
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_collect_one_location, db_path, loc,
                                   primary_id): loc for loc in locations}
            for fut in as_completed(futures):
                loc = futures[fut]
                try:
                    r = fut.result()
                except Exception as e:  # noqa: BLE001 - hard failure, isolated
                    log.exception("location %s hard-failed", loc["name"])
                    hard_failures.append(f"location:{loc['name']}")
                    r = {"name": loc["name"], "location_id": loc["id"],
                         "status": "failed",
                         "error": f"{type(e).__name__}: {e}",
                         "failed_steps": [], "rows_written": 0,
                         "validation_rejected": 0,
                         "validation_rejected_by_field": {},
                         "started_at": started_at, "ended_at": utc_iso(),
                         "duration_s": 0.0, "result": {}}
                loc_results[r["name"]] = r
                for step in r["failed_steps"]:
                    source_failures.append(f"{r['name']}:{step}")
                _ledger_child(conn, run_id, "location", f"location:{r['name']}",
                              r["started_at"], r["ended_at"], r["status"],
                              r["rows_written"],
                              r.get("error") or (
                                  "failed steps: " + ", ".join(r["failed_steps"])
                                  if r["failed_steps"] else None))

        # ---- 2b. one retry wave for lock-failed locations ---------------
        # Contention on this VM arrives in waves (sibling stage bursts,
        # checkpoint flushes, cold caches after restarts): a location
        # whose store steps died on "database is locked" usually lands
        # cleanly a few tens of seconds later. Retry those locations
        # ONCE inside the run instead of leaving them to the next run's
        # hour-guard, which may face the same wave (added 2026-10-18
        # after repeated storm runs lost 100+ locations for an hour).
        def _lock_failed(r: Dict[str, Any]) -> bool:
            if "database is locked" in str(r.get("error") or ""):
                return True
            res = r.get("result") or {}
            return any("database is locked" in str((res.get(s) or {}).get("error") or "")
                       for s in r.get("failed_steps") or [])

        retry_names = [n for n, r in loc_results.items()
                       if r["status"] in ("partial", "failed") and _lock_failed(r)]
        if retry_names:
            log.info("retry wave: %d lock-failed locations", len(retry_names))
            time.sleep(20)
            by_name = {loc["name"]: loc for loc in locations}
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {pool.submit(_collect_one_location, db_path,
                                       by_name[n], primary_id): n
                           for n in retry_names}
                for fut in as_completed(futures):
                    n = futures[fut]
                    try:
                        r2 = fut.result()
                    except Exception as e:  # noqa: BLE001 - keep first result
                        log.warning("retry for %s hard-failed: %s", n, e)
                        continue
                    old = loc_results[n]
                    source_failures[:] = [x for x in source_failures
                                          if not x.startswith(n + ":")]
                    if old["status"] == "failed":
                        hard_failures[:] = [x for x in hard_failures
                                            if x != f"location:{n}"]
                    r2["rows_written"] = (old["rows_written"]
                                          + r2["rows_written"])
                    loc_results[n] = r2
                    for step in r2["failed_steps"]:
                        source_failures.append(f"{n}:{step}")
                    _ledger_child(conn, run_id, "location",
                                  f"location:{n}#retry",
                                  r2["started_at"], r2["ended_at"],
                                  r2["status"], r2["rows_written"],
                                  r2.get("error") or (
                                      "failed steps: " + ", ".join(r2["failed_steps"])
                                      if r2["failed_steps"] else None))
            summary["retry_wave"] = {
                "attempted": sorted(retry_names),
                "recovered": sorted(n for n in retry_names
                                    if loc_results[n]["status"] == "ok"),
            }
        validation = {
            "rejected": sum(r["validation_rejected"] for r in loc_results.values()),
            "by_field": {},
            "by_location": {r["name"]: r["validation_rejected"]
                            for r in loc_results.values()
                            if r["validation_rejected"]},
        }
        for r in loc_results.values():
            for f, n in r["validation_rejected_by_field"].items():
                validation["by_field"][f] = validation["by_field"].get(f, 0) + n
        summary["locations"] = {
            "total": len(locations),
            "collected": sorted(n for n, r in loc_results.items()
                                if r["status"] in ("ok", "partial")),
            "skipped": sorted(n for n, r in loc_results.items()
                              if r["status"] == "skipped"),
            "failed": sorted(n for n, r in loc_results.items()
                             if r["status"] == "failed"),
            "results": {n: {k: r[k] for k in
                            ("status", "rows_written", "validation_rejected",
                             "duration_s", "failed_steps")}
                        for n, r in loc_results.items()},
        }
        summary["validation"] = validation

        # ---- 3. gap detection + backfill --------------------------------
        gap_summary: Dict[str, Any] = {"found": 0, "fillable": 0, "filled": 0,
                                       "remaining": 0, "backfilled_locations": []}
        try:
            now = now_utc()
            gaps_before = detect_all_gaps(conn, locations, gap_window_hours, now)
            found = sum(len(m) for per in gaps_before.values() for m in per.values())
            # The forecast fetch starts at the current day 00:00 UTC; only
            # gaps at/after that floor can be filled by a re-fetch.
            floor = utc_iso(datetime(now.year, now.month, now.day,
                                     tzinfo=timezone.utc))
            fillable = sum(1 for per in gaps_before.values()
                           for missing in per.values()
                           for h in missing if h >= floor)
            gap_summary["found"] = found
            gap_summary["fillable"] = fillable
            backfill_rejects = 0
            if backfill and fillable:
                targets = [loc for loc in locations
                           if loc["name"] in gaps_before and any(
                               h >= floor
                               for missing in gaps_before[loc["name"]].values()
                               for h in missing)]
                with ThreadPoolExecutor(max_workers=max_workers) as pool:
                    futures = {pool.submit(_backfill_location, db_path, loc,
                                           primary_id): loc for loc in targets}
                    for fut in as_completed(futures):
                        loc = futures[fut]
                        b_started = utc_iso()
                        try:
                            br = fut.result()
                            backfill_rejects += br["validation_rejected"]
                            gap_summary["backfilled_locations"].append(loc["name"])
                            _ledger_child(
                                conn, run_id, "backfill", f"backfill:{loc['name']}",
                                b_started, utc_iso(), br["status"], 0,
                                "failed models: " + ", ".join(br["failed_models"])
                                if br["failed_models"] else None)
                        except Exception as e:  # noqa: BLE001
                            log.exception("backfill %s failed", loc["name"])
                            source_failures.append(f"backfill:{loc['name']}")
                            _ledger_child(conn, run_id, "backfill",
                                          f"backfill:{loc['name']}", b_started,
                                          utc_iso(), "failed", 0,
                                          f"{type(e).__name__}: {e}")
                gaps_after = detect_all_gaps(conn, locations, gap_window_hours, now)
                remaining = sum(len(m) for per in gaps_after.values()
                                for m in per.values())
                gap_summary["remaining"] = remaining
                gap_summary["filled"] = found - remaining
            else:
                gap_summary["remaining"] = found
            if backfill_rejects:
                validation["rejected"] += backfill_rejects
                validation["during_backfill"] = backfill_rejects
        except Exception as e:  # noqa: BLE001
            hard_failures.append("gap_detection")
            log.exception("gap phase crashed: %s", e)
            gap_summary["error"] = f"{type(e).__name__}: {e}"
        summary["gaps"] = gap_summary

        # ---- finalize ----------------------------------------------------
        after = _rc.counts(conn)
        rows_written = sum(max(after[t] - before[t], 0) for t in _rc.TABLES)
        summary["rows_written"] = rows_written
        summary["table_deltas"] = {t: after[t] - before[t] for t in _rc.TABLES}
    finally:
        ended_at = utc_iso()
        status = ("failed" if hard_failures else
                  "partial" if source_failures else "ok")
        try:
            _ledger_finish(conn, run_id, status,
                           summary.get("rows_written", 0),
                           "; ".join(hard_failures + source_failures) or None,
                           ended_at)
        finally:
            conn.close()
    summary["ended_at"] = ended_at
    summary["duration_s"] = round(time.monotonic() - t_run, 3)
    summary["status"] = status
    summary["hard_failures"] = hard_failures
    summary["source_failures"] = source_failures
    summary["ledger"] = {"run_id": run_id, "component": "advanced_collect",
                         "status": status}
    return summary
