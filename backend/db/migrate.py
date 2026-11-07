"""Idempotent additive migrations for the AETHER DB (v2 additions).

schema.sql is the source of truth for NEW databases (CREATE TABLE IF NOT
EXISTS). This module brings EXISTING databases up to date with additive
changes only: ALTER TABLE ADD COLUMN / CREATE TABLE IF NOT EXISTS. It never
drops, renames, or rewrites data.
"""
from __future__ import annotations

import sqlite3

ADD_COLUMNS = {
    # table -> [(column, ddl), ...]
    "weather_forecasts": [
        ("dew_point", "REAL"),
        ("wind_gust", "REAL"),
        ("pm2_5", "REAL"),
        ("pm10", "REAL"),
        ("nitrogen_dioxide", "REAL"),
        ("ozone", "REAL"),
        ("sulphur_dioxide", "REAL"),
        ("carbon_monoxide", "REAL"),
    ],
    "tsunami_messages": [
        ("version", "INTEGER"),
    ],
    # Collection cadence tier (2026-10-17 worldwide scale-up): 1 = hourly
    # (Tier A: capitals + largest cities), 6 = every 6 hours (Tier B).
    "locations": [
        ("collect_every_hours", "INTEGER NOT NULL DEFAULT 1"),
    ],
}

NEW_TABLES = {
    "weather_daily": """CREATE TABLE IF NOT EXISTS weather_daily (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    location_id INTEGER NOT NULL REFERENCES locations(id),
    source TEXT NOT NULL,
    model TEXT NOT NULL,
    date TEXT NOT NULL,
    sunrise TEXT, sunset TEXT,
    collected_at TEXT NOT NULL,
    UNIQUE(location_id, source, model, date)
)""",
    "ocean_forecasts": """CREATE TABLE IF NOT EXISTS ocean_forecasts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    location_id INTEGER NOT NULL REFERENCES locations(id),
    source TEXT NOT NULL,
    model TEXT NOT NULL,
    valid_time TEXT NOT NULL,
    wave_height REAL,
    wave_period REAL,
    wave_direction REAL,
    sea_surface_temperature REAL,
    collected_at TEXT NOT NULL,
    UNIQUE(location_id, source, model, valid_time)
)""",
}

NEW_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_ocean_loc_time ON ocean_forecasts(location_id, valid_time)",
    "CREATE INDEX IF NOT EXISTS idx_wdaily_loc ON weather_daily(location_id, date)",
]


def ensure_v2(conn: sqlite3.Connection) -> dict:
    """Apply v2 additive migrations idempotently. Returns what was applied."""
    applied = {"columns": [], "tables": [], "indexes": []}
    for table, cols in ADD_COLUMNS.items():
        # PRAGMA table_info on a missing table returns zero rows (no error),
        # so check existence explicitly before attempting ALTER.
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,)).fetchone()
        if not exists:
            continue  # table missing entirely; schema.sql owns creation
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, ddl in cols:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
                applied["columns"].append(f"{table}.{name}")
    for name, ddl in NEW_TABLES.items():
        conn.execute(ddl)
        applied["tables"].append(name)
    for idx in NEW_INDEXES:
        conn.execute(idx)
        applied["indexes"].append(idx.split("IF NOT EXISTS ")[1].split(" ON")[0])
    conn.commit()
    return applied
