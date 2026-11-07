"""Ensemble engine (spec §22).

Computes ensemble statistics — mean, median, quartiles, spread — from the
NWP models actually collected in the DB for a location. Real data only;
kind=MODEL OUTPUT. No model is privileged: every collected model contributes
equally to the raw ensemble (weights are the separate dynamic_weights engine).
"""
from __future__ import annotations

import sqlite3
import statistics
from typing import Any, Dict, List, Optional

from backend.common.timeutil import sql_now_iso

VARIABLES = ["temperature", "humidity", "pressure", "wind_speed", "wind_direction",
             "precipitation", "precip_probability", "cloud_cover", "uv"]


def _quartiles(sorted_vals: List[float]) -> Dict[str, float]:
    q = statistics.quantiles(sorted_vals, n=4, method="exclusive")
    return {"q25": round(q[0], 2), "median": round(statistics.median(sorted_vals), 2),
            "q75": round(q[2], 2)}


def ensemble_for_location(conn: sqlite3.Connection, location_id: int,
                          hours: int = 48,
                          models: Optional[List[str]] = None,
                          variables: Optional[List[str]] = None) -> Dict[str, Any]:
    """Ensemble stats per valid_time across collected models."""
    models = models or ["gfs_seamless", "icon_seamless", "ecmwf_ifs", "jma_seamless"]
    variables = [v for v in (variables or VARIABLES) if v in VARIABLES]
    placeholders = ",".join("?" * len(models))
    cols = ", ".join(variables)
    now_m2h = sql_now_iso("-2 hours")
    now_ph = sql_now_iso(f"+{hours} hours")
    rows = conn.execute(
        f"""SELECT valid_time, model, {cols}, collected_at FROM weather_forecasts
            WHERE location_id = ? AND source = 'open-meteo' AND model IN ({placeholders})
              AND valid_time BETWEEN {now_m2h} AND {now_ph} ORDER BY valid_time""",
        (location_id, *models)).fetchall()

    by_time: Dict[str, Dict[str, List[float]]] = {}
    by_time_models: Dict[str, set] = {}
    latest_collect = None
    for r in rows:
        r = dict(r)
        vt = r["valid_time"]
        by_time.setdefault(vt, {v: [] for v in variables})
        by_time_models.setdefault(vt, set()).add(r["model"])
        for v in variables:
            if r[v] is not None:
                by_time[vt][v].append(float(r[v]))
        if r.get("collected_at") and (latest_collect is None or r["collected_at"] > latest_collect):
            latest_collect = r["collected_at"]

    items = []
    for vt in sorted(by_time):
        entry: Dict[str, Any] = {"valid_time": vt, "kind": "MODEL OUTPUT",
                                 "n_models": len(by_time_models[vt]),
                                 "models": sorted(by_time_models[vt]),
                                 "variables": {}}
        for v in variables:
            vals = sorted(by_time[vt][v])
            if not vals:
                entry["variables"][v] = None
                continue
            stats = {"mean": round(statistics.fmean(vals), 2),
                     "spread": round(max(vals) - min(vals), 2),
                     "min": round(vals[0], 2), "max": round(vals[-1], 2),
                     "n": len(vals)}
            if len(vals) >= 2:
                stats.update(_quartiles(vals))
            entry["variables"][v] = stats
        items.append(entry)

    return {
        "location_id": location_id,
        "count": len(items),
        "items": items,
        "kind": "MODEL OUTPUT",
        "note": ("Equal-weight ensemble across collected NWP models. "
                 "For evidence-based weighting see /v1/engines/weights."),
        "provenance": {"sources": ["open-meteo/" + m for m in models],
                       "updated_at": latest_collect},
    }
