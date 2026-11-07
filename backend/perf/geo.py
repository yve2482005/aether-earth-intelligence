"""H3 spatial indexing (§16): assign H3 cells to locations / forecast grid points.

Shares processed per-cell forecasts: grid points inside the same H3 cell reuse
one cell-level aggregate instead of re-interpolating per point. Default
resolution 7 (~5 km edge) for weather cells; resolution 5 for map clustering.

SCALING PATH (not deployed): PostGIS with native H3/geography indexes.
Documented in docs/PERFORMANCE.md. SQLite is the deployment here; this module
uses the h3 Python library (free) and stores cell ids as TEXT.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, Iterable, List, Optional, Tuple

import h3

from backend.common.logging_setup import setup_logging

log = setup_logging("aether.perf.geo")

WEATHER_RES = 7
CLUSTER_RES = 5


def cell_for(lat: float, lon: float, res: int = WEATHER_RES) -> str:
    return h3.latlng_to_cell(lat, lon, res)


def cell_center(cell: str) -> Tuple[float, float]:
    return h3.cell_to_latlng(cell)


def neighbors(cell: str, k: int = 1) -> List[str]:
    return list(h3.grid_disk(cell, k))


def parent_cell(cell: str, res: int) -> str:
    return h3.cell_to_parent(cell, res)


def ensure_cell_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS perf_h3_cells (
            cell TEXT PRIMARY KEY,
            res INTEGER NOT NULL,
            center_lat REAL NOT NULL,
            center_lon REAL NOT NULL,
            updated_at TEXT NOT NULL
        )""")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS perf_location_cells (
            location_id INTEGER PRIMARY KEY,
            cell_res7 TEXT NOT NULL,
            cell_res5 TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""")
    conn.commit()


def assign_location(conn: sqlite3.Connection, location_id: int, lat: float,
                    lon: float, updated_at: str) -> Dict[str, str]:
    """Assign (and persist) H3 cells to a location."""
    ensure_cell_table(conn)
    c7 = cell_for(lat, lon, WEATHER_RES)
    c5 = cell_for(lat, lon, CLUSTER_RES)
    conn.execute(
        """INSERT INTO perf_location_cells (location_id, cell_res7, cell_res5, updated_at)
           VALUES (?,?,?,?)
           ON CONFLICT(location_id) DO UPDATE SET
             cell_res7=excluded.cell_res7, cell_res5=excluded.cell_res5,
             updated_at=excluded.updated_at""",
        (location_id, c7, c5, updated_at))
    for cell, res in ((c7, WEATHER_RES), (c5, CLUSTER_RES)):
        clat, clon = cell_center(cell)
        conn.execute(
            """INSERT INTO perf_h3_cells (cell, res, center_lat, center_lon, updated_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(cell) DO UPDATE SET updated_at=excluded.updated_at""",
            (cell, res, clat, clon, updated_at))
    conn.commit()
    return {"cell_res7": c7, "cell_res5": c5}


def cells_for_locations(conn: sqlite3.Connection) -> Dict[int, Dict[str, str]]:
    ensure_cell_table(conn)
    return {r["location_id"]: {"cell_res7": r["cell_res7"], "cell_res5": r["cell_res5"]}
            for r in conn.execute("SELECT * FROM perf_location_cells")}


def cluster_by_cell(events: List[Dict[str, Any]],
                    res: int = CLUSTER_RES) -> Dict[str, List[Dict[str, Any]]]:
    """Group events by H3 cell for zoom-dependent map clustering (§18/72)."""
    clusters: Dict[str, List[Dict[str, Any]]] = {}
    for e in events:
        lat, lon = e.get("latitude"), e.get("longitude")
        if lat is None or lon is None:
            continue
        cell = cell_for(float(lat), float(lon), res)
        clusters.setdefault(cell, []).append(e)
    return clusters


def cluster_summary(cell: str, events: List[Dict[str, Any]]) -> Dict[str, Any]:
    clat, clon = cell_center(cell)
    mags = [e.get("magnitude") for e in events if e.get("magnitude") is not None]
    return {"cell": cell, "count": len(events),
            "center": {"latitude": clat, "longitude": clon},
            "max_magnitude": max(mags) if mags else None,
            "top_event_id": events[0].get("canonical_event_id") or events[0].get("event_id")}


def viewport_cull(events: List[Dict[str, Any]], bbox: Tuple[float, float, float, float]) -> List[Dict[str, Any]]:
    """Keep events inside (min_lon, min_lat, max_lon, max_lat)."""
    min_lon, min_lat, max_lon, max_lat = bbox
    return [e for e in events
            if e.get("latitude") is not None and e.get("longitude") is not None
            and min_lat <= float(e["latitude"]) <= max_lat
            and min_lon <= float(e["longitude"]) <= max_lon]
