"""Perf tests: H3 geo (§16) + viewport/map helpers (§§18, 72). Hermetic."""
from __future__ import annotations

import sqlite3

from backend.perf import geo


def test_cell_round_trip():
    cell = geo.cell_for(16.84, 96.17, res=7)
    assert isinstance(cell, str) and len(cell) > 0
    lat, lon = geo.cell_center(cell)
    assert abs(lat - 16.84) < 0.05 and abs(lon - 96.17) < 0.05


def test_same_location_same_cell_different_locations_differ():
    a = geo.cell_for(16.84, 96.17)
    b = geo.cell_for(16.841, 96.171)   # ~150 m away: same res-7 cell
    c = geo.cell_for(21.95, 96.08)     # Mandalay: different cell
    assert a == b and a != c


def test_assign_location_persists(tmp_path):
    db = str(tmp_path / "g.db")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    out = geo.assign_location(conn, 1, 16.84, 96.17, "2026-10-05T00:00:00Z")
    assert out["cell_res7"] == geo.cell_for(16.84, 96.17)
    assert out["cell_res5"] == geo.cell_for(16.84, 96.17, res=5)
    got = geo.cells_for_locations(conn)
    assert got[1]["cell_res7"] == out["cell_res7"]
    conn.close()


def _events():
    return [
        {"event_id": "e1", "latitude": 16.8, "longitude": 96.1, "magnitude": 5.0},
        {"event_id": "e2", "latitude": 16.81, "longitude": 96.11, "magnitude": 6.1},
        {"event_id": "e3", "latitude": 21.9, "longitude": 96.0, "magnitude": 4.2},
        {"event_id": "e4", "latitude": None, "longitude": None, "magnitude": 3.0},
    ]


def test_viewport_cull():
    culled = geo.viewport_cull(_events(), (95.0, 16.0, 97.0, 17.5))
    assert {e["event_id"] for e in culled} == {"e1", "e2"}


def test_cluster_by_cell_groups_nearby():
    clusters = geo.cluster_by_cell(_events(), res=5)
    sizes = sorted(len(v) for v in clusters.values())
    assert sizes[-1] >= 2  # e1+e2 cluster together
    assert sum(sizes) == 3  # e4 (no coords) excluded


def test_cluster_summary():
    clusters = geo.cluster_by_cell(_events(), res=5)
    big = max(clusters.values(), key=len)
    cell = next(c for c, v in clusters.items() if v is big)
    s = geo.cluster_summary(cell, big)
    assert s["count"] == len(big)
    assert s["max_magnitude"] == 6.1
    assert "center" in s


def test_parent_cell_aggregation():
    child = geo.cell_for(16.84, 96.17, res=7)
    parent = geo.parent_cell(child, 5)
    assert parent == geo.cell_for(16.84, 96.17, res=5)
