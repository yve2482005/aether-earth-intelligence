"""Idempotent ingestion (§10): content-hash dedup keys.

Where cheap, dedup keys are sha256 over a canonical serialization of the
record's identity fields — not the row id, not the arrival order. Re-ingesting
the same logical event produces zero new rows. (USGS events already carry
stable source ids; the content hash is the second, cross-source line of
defense used by the fast path.)

Canonical serialization: sorted keys, compact separators, UTF-8 — two
records with the same content hash to the same bytes even if the dicts were
built in different field order.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Iterable, Optional, Sequence

from backend.common.logging_setup import setup_logging

log = setup_logging("aether.perf.ids")


def content_hash(fields: Dict[str, Any]) -> str:
    """sha256 over canonical JSON of the identity fields."""
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def quake_identity(source: str, source_event_id: str, origin_time: str,
                   magnitude: Optional[float], latitude: Optional[float],
                   longitude: Optional[float]) -> str:
    """Content-hash key for a quake record. Two collectors seeing the same
    USGS event produce the same key even if they disagree on ordering."""
    return content_hash({
        "kind": "quake",
        "source": source,
        "source_event_id": source_event_id,
        "origin_time": str(origin_time),
        "magnitude": round(float(magnitude), 2) if magnitude is not None else None,
        "latitude": round(float(latitude), 4) if latitude is not None else None,
        "longitude": round(float(longitude), 4) if longitude is not None else None,
    })


def alert_identity(alert_type: str, location_id: int, title: str,
                   issued_at: str) -> str:
    return content_hash({
        "kind": "alert", "type": alert_type, "location_id": location_id,
        "title": title, "issued_at": str(issued_at),
    })


def already_ingested(conn, table: str, hash_column: str, h: str) -> bool:
    row = conn.execute(
        f"SELECT 1 FROM {table} WHERE {hash_column}=? LIMIT 1", (h,)).fetchone()
    return row is not None


def insert_once(conn, table: str, hash_column: str, h: str,
                columns: Sequence[str], values: Sequence[Any]) -> bool:
    """INSERT unless the content hash is already present. Returns True if the
    row was actually inserted (False = duplicate skipped)."""
    if already_ingested(conn, table, hash_column, h):
        return False
    cols = ", ".join(list(columns) + [hash_column])
    placeholders = ", ".join(["?"] * (len(values) + 1))
    conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({placeholders})",
                 (*values, h))
    conn.commit()
    return True
