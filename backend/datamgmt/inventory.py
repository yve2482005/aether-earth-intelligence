"""Table inventory: every table in sqlite_master with row counts, column
definitions, per-table size estimates (dbstat when available) and the
value range of an obvious time column when one exists.

All identifiers interpolated into SQL here come from sqlite_master /
PRAGMA output, never from user input; user-supplied table names are
validated against sqlite_master before use (see require_table).
"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional

# Candidate "primary time" columns, in priority order. The first four are
# the canonical event-time columns used across AETHER schemas; the rest
# cover tables whose event time is named differently (observations,
# prediction runs, message receipt, ingestion runs, ...).
TIME_COLUMNS = (
    "valid_time", "origin_time", "created_at", "issued_at",
    "observed_at", "issue_time", "predicted_at", "collected_at",
    "received_at", "update_time", "checked_at", "started_at",
)


def list_tables(conn: sqlite3.Connection) -> List[str]:
    """All real table names (excludes sqlite internals except sqlite_stat*
    which are genuine tables once ANALYZE has run; keep them, they are
    part of the database)."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
    return [r[0] for r in rows]


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone()
    return row is not None


def require_table(conn: sqlite3.Connection, name: str) -> str:
    """Validate a user-supplied table name against sqlite_master.

    Returns the canonical name or raises ValueError. This is the
    injection guard for every code path that must interpolate a table
    name into SQL (SQLite cannot bind identifiers as parameters).
    """
    if not table_exists(conn, name):
        raise ValueError(f"unknown table: {name!r}")
    return name


def table_columns(conn: sqlite3.Connection, name: str) -> List[Dict[str, Any]]:
    require_table(conn, name)
    cols = []
    for r in conn.execute(f'PRAGMA table_info("{name}")').fetchall():
        cols.append({"name": r[1], "type": r[2] or "", "notnull": bool(r[3]),
                     "primary_key": bool(r[5])})
    return cols


def column_names(conn: sqlite3.Connection, name: str) -> List[str]:
    return [c["name"] for c in table_columns(conn, name)]


def pick_time_column(columns: List[str]) -> Optional[str]:
    for cand in TIME_COLUMNS:
        if cand in columns:
            return cand
    return None


def _dbstat_sizes(conn: sqlite3.Connection) -> Optional[Dict[str, int]]:
    """Per-name byte sizes from the dbstat virtual table, or None."""
    try:
        rows = conn.execute(
            "SELECT name, SUM(pgsize) FROM dbstat GROUP BY name").fetchall()
    except sqlite3.Error:
        return None
    return {r[0]: int(r[1]) for r in rows}


def table_inventory(conn: sqlite3.Connection, name: str) -> Dict[str, Any]:
    require_table(conn, name)
    cols = table_columns(conn, name)
    col_names = [c["name"] for c in cols]
    count = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
    time_col = pick_time_column(col_names)
    tmin = tmax = None
    if time_col:
        tmin, tmax = conn.execute(
            f'SELECT MIN("{time_col}"), MAX("{time_col}") FROM "{name}"'
        ).fetchone()
    return {
        "table": name,
        "row_count": count,
        "columns": cols,
        "column_count": len(cols),
        "time_column": time_col,
        "time_min": tmin,
        "time_max": tmax,
    }


def full_inventory(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Inventory of every table, with dbstat sizes attached when the
    dbstat virtual table is available (size_bytes then covers the table
    and its indexes under their own names; the per-table figure here is
    the table's own pages only)."""
    sizes = _dbstat_sizes(conn)
    tables = []
    for name in list_tables(conn):
        info = table_inventory(conn, name)
        info["size_bytes"] = sizes.get(name) if sizes is not None else None
        tables.append(info)
    return {
        "tables": tables,
        "table_count": len(tables),
        "total_rows": sum(t["row_count"] for t in tables),
        "dbstat_available": sizes is not None,
    }
