"""Per-dataset data quality: row counts, NULL rates on key columns,
duplicate natural-key counts and latest-timestamp freshness for the key
AETHER datasets.

Key columns and natural keys are declared per dataset below; every
column actually used is re-validated against the live schema, so a
dataset whose table is missing (or a column renamed) reports what is
there instead of raising.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional

from backend.common.timeutil import age_minutes
from backend.datamgmt.inventory import column_names, table_exists

# dataset -> (key columns for NULL-rate, natural key columns or None,
#             primary event-time column for freshness)
DATASETS: Dict[str, Dict[str, Any]] = {
    "weather_forecasts": {
        "key_columns": ["location_id", "model", "forecast_run",
                        "valid_time", "temperature"],
        "natural_key": ["location_id", "model", "forecast_run", "valid_time"],
        "time_column": "valid_time",
    },
    "weather_observations": {
        "key_columns": ["location_id", "observed_at", "temperature", "source"],
        "natural_key": ["location_id", "station", "observed_at"],
        "time_column": "observed_at",
    },
    "earthquake_events": {
        "key_columns": ["canonical_event_id", "origin_time", "magnitude",
                        "latitude", "longitude"],
        "natural_key": ["canonical_event_id"],
        "time_column": "origin_time",
    },
    "tsunami_messages": {
        "key_columns": ["tsunami_event_id", "message_type", "issue_time"],
        "natural_key": ["tsunami_event_id", "message_type", "version"],
        "time_column": "issue_time",
    },
    "alerts": {
        "key_columns": ["alert_id", "type", "severity", "issued_at", "status"],
        "natural_key": ["alert_id"],
        "time_column": "issued_at",
    },
    "ml_batch_predictions": {
        "key_columns": ["task", "model_version", "location_id", "valid_time",
                        "prediction"],
        "natural_key": ["task", "model_version", "location_id", "valid_time"],
        "time_column": "valid_time",
    },
}


def _null_rates(conn: sqlite3.Connection, table: str,
                columns: List[str], total: int) -> Dict[str, Optional[float]]:
    rates: Dict[str, Optional[float]] = {}
    for col in columns:
        if total == 0:
            rates[col] = None
            continue
        non_null = conn.execute(
            f'SELECT COUNT("{col}") FROM "{table}"').fetchone()[0]
        rates[col] = round((total - non_null) / total, 4)
    return rates


def _duplicate_count(conn: sqlite3.Connection, table: str,
                     key_cols: List[str], total: int) -> Optional[int]:
    """Rows beyond the first sharing a natural key (total - distinct keys).

    Returns None when any key column is absent from the live schema.
    """
    cols = ", ".join(f'"{c}"' for c in key_cols)
    distinct = conn.execute(
        f'SELECT COUNT(*) FROM (SELECT DISTINCT {cols} FROM "{table}")'
    ).fetchone()[0]
    return total - distinct


def dataset_quality(conn: sqlite3.Connection, table: str,
                    spec: Dict[str, Any]) -> Dict[str, Any]:
    if not table_exists(conn, table):
        return {"dataset": table, "present": False, "row_count": None,
                "null_rates": {}, "duplicate_natural_key_rows": None,
                "latest_timestamp": None, "latest_column": None,
                "age_minutes": None}
    live_cols = column_names(conn, table)
    total = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]

    key_cols = [c for c in spec["key_columns"] if c in live_cols]
    null_rates = _null_rates(conn, table, key_cols, total)

    nat_key: Optional[List[str]] = spec.get("natural_key")
    dup = None
    if nat_key and all(c in live_cols for c in nat_key):
        dup = _duplicate_count(conn, table, nat_key, total)

    time_col = spec.get("time_column")
    latest = None
    if time_col and time_col in live_cols:
        latest = conn.execute(
            f'SELECT MAX("{time_col}") FROM "{table}"').fetchone()[0]
    return {
        "dataset": table,
        "present": True,
        "row_count": total,
        "key_columns": key_cols,
        "null_rates": null_rates,
        "natural_key": nat_key,
        "duplicate_natural_key_rows": dup,
        "latest_column": time_col if latest is not None else None,
        "latest_timestamp": latest,
        "age_minutes": (round(age_minutes(latest), 1)
                        if latest and age_minutes(latest) is not None else None),
    }


def all_quality(conn: sqlite3.Connection) -> Dict[str, Any]:
    return {name: dataset_quality(conn, name, spec)
            for name, spec in DATASETS.items()}
