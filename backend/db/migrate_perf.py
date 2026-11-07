"""Perf migration (§31): ADDITIVE indexes + perf tables. Never touches schema.sql.

Separate file (owned by workstream F) so it never conflicts with
migrate_v2.py (workstream E). All CREATE ... IF NOT EXISTS — safe to run
repeatedly, safe alongside the hourly cron.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Dict, Optional

from backend.common.logging_setup import setup_logging

log = setup_logging("aether.perf.migrate")

# Indexes on the hot query paths. Each one targets a real observed access
# pattern (see docs/PERFORMANCE.md §92 for the EXPLAIN QUERY PLAN evidence):
INDEXES = [
    # earthquake_events: time-range scans (recent events, quake proximity)
    ("idx_perf_eq_origin_time", "earthquake_events(origin_time)"),
    ("idx_perf_eq_mag_time", "earthquake_events(magnitude, origin_time)"),
    ("idx_perf_eq_source", "earthquake_events(source)"),
    # earthquake_updates: per-event update joins
    ("idx_perf_equ_event", "earthquake_updates(canonical_event_id, update_time)"),
    # tsunami_events: active-message scans
    ("idx_perf_ts_status_time", "tsunami_events(status, issue_time)"),
    ("idx_perf_ts_sev", "tsunami_events(severity)"),
    # alerts: status + severity ordering for the home bundle + engine scans
    ("idx_perf_alerts_status_sev", "alerts(status, severity, issued_at)"),
    ("idx_perf_alerts_loc", "alerts(location_id, status)"),
    # weather_forecasts: bundle hourly fetch (location, model, time)
    ("idx_perf_wf_loc_model_time", "weather_forecasts(location_id, model, valid_time)"),
    # weather_daily: bundle daily fetch
    ("idx_perf_wd_loc_date", "weather_daily(location_id, date)"),
    # weather_observations: latest-obs lookups
    ("idx_perf_wo_loc_time", "weather_observations(location_id, observed_at)"),
    # system_health: freshness lookups per component
    ("idx_perf_sh_comp_time", "system_health(component, checked_at)"),
    # earth_events: type/time scans
    ("idx_perf_ee_type_time", "earth_events(event_type, start_time)"),
    # notification_history: per-alert delivery lookups
    ("idx_perf_nh_alert", "notification_history(alert_id)"),
    # ingestion_runs: component status
    ("idx_perf_ir_comp", "ingestion_runs(component, started_at)"),
    # data_quality: component checks
    ("idx_perf_dq_comp", "data_quality(component, checked_at)"),
    # locations: lat/lon proximity lookups
    ("idx_perf_loc_latlon", "locations(latitude, longitude)"),
]

PERF_TABLES = [
    """CREATE TABLE IF NOT EXISTS perf_source_schedule (
        source TEXT PRIMARY KEY,
        interval_seconds INTEGER NOT NULL,
        last_started_at TEXT,
        last_finished_at TEXT,
        last_status TEXT,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS http_cache_meta (
        url TEXT PRIMARY KEY,
        etag TEXT,
        last_modified TEXT,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS perf_ingest_cursors (
        cursor_key TEXT PRIMARY KEY,
        last_seen_value TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS cache_entries (
        key TEXT PRIMARY KEY,
        value_json TEXT NOT NULL,
        stored_at REAL NOT NULL,
        expires_at REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS perf_home_bundles (
        location_id INTEGER PRIMARY KEY,
        level TEXT NOT NULL,
        bundle_json TEXT NOT NULL,
        built_at TEXT NOT NULL,
        valid_until TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS perf_h3_cells (
        cell TEXT PRIMARY KEY,
        res INTEGER NOT NULL,
        center_lat REAL NOT NULL,
        center_lon REAL NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS perf_location_cells (
        location_id INTEGER PRIMARY KEY,
        cell_res7 TEXT NOT NULL,
        cell_res5 TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS notify_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        lane TEXT NOT NULL,
        alert_id TEXT,
        title TEXT NOT NULL,
        body TEXT NOT NULL,
        payload_json TEXT,
        status TEXT NOT NULL DEFAULT 'queued',
        attempts INTEGER NOT NULL DEFAULT 0,
        stale INTEGER NOT NULL DEFAULT 0,
        error TEXT,
        enqueued_at TEXT NOT NULL,
        delivered_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS ml_batch_predictions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task TEXT NOT NULL,
        model_version TEXT NOT NULL,
        location_id INTEGER,
        valid_time TEXT NOT NULL,
        prediction REAL NOT NULL,
        features_hash TEXT,
        predicted_at TEXT NOT NULL,
        UNIQUE(task, model_version, location_id, valid_time)
    )""",
    """CREATE TABLE IF NOT EXISTS perf_metrics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        stage TEXT NOT NULL, label TEXT NOT NULL,
        n INTEGER NOT NULL, failures INTEGER NOT NULL,
        p50_ms REAL, p95_ms REAL, p99_ms REAL, max_ms REAL, mean_ms REAL,
        recorded_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS perf_slow_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,                -- backfill|training|backtest|verify_report|drive_backup
        status TEXT NOT NULL DEFAULT 'queued',  -- queued|running|done|failed
        started_at TEXT, finished_at TEXT,
        detail TEXT, error TEXT,
        created_at TEXT NOT NULL
    )""",
]


def apply(path: Optional[str | Path] = None) -> Dict[str, int]:
    from backend.db import db as dbmod
    conn = dbmod.connect(path)
    try:
        return apply_conn(conn)
    finally:
        conn.close()


def apply_conn(conn: sqlite3.Connection) -> Dict[str, int]:
    n_tables = 0
    for ddl in PERF_TABLES:
        conn.execute(ddl)
        n_tables += 1
    n_indexes = 0
    for name, spec in INDEXES:
        tbl = spec.split("(")[0].strip()
        try:
            conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {tbl}({spec.split('(', 1)[1]}")
            n_indexes += 1
        except sqlite3.OperationalError as e:
            # table/column may not exist on an older schema — honest skip
            log.warning("index %s skipped: %s", name, e)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_nq_lane_status ON notify_queue(lane, status, id)")
    conn.commit()
    return {"tables": n_tables, "indexes": n_indexes}


if __name__ == "__main__":
    res = apply()
    print(res)
