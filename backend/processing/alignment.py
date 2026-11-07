"""Alignment: pair forecast rows with observation rows for the same location/time.

Used by the ML team to build training datasets (forecast error per horizon).
Read-only w.r.t. the DB: takes row dicts, returns aligned dicts.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from backend.common.timeutil import parse_iso, utc_iso


def align_forecast_observation(forecasts: List[Dict[str, Any]],
                               observations: List[Dict[str, Any]],
                               tolerance_min: float = 45.0) -> List[Dict[str, Any]]:
    """For each forecast valid_time, find the nearest observation within tolerance.

    Returns list of {valid_time, model, forecast fields..., observed fields...,
    lead_hours, obs_gap_min}. Forecasts with no observation in tolerance are skipped.
    """
    obs_sorted = sorted(observations, key=lambda o: o.get("observed_at") or "")
    aligned: List[Dict[str, Any]] = []
    for f in forecasts:
        fdt = parse_iso(f.get("valid_time"))
        rdt = parse_iso(f.get("forecast_run"))
        if fdt is None:
            continue
        best: Optional[Dict[str, Any]] = None
        best_gap = float("inf")
        for o in obs_sorted:
            odt = parse_iso(o.get("observed_at"))
            if odt is None:
                continue
            gap = abs((fdt - odt).total_seconds() / 60.0)
            if gap < best_gap:
                best_gap, best = gap, o
            elif odt > fdt and gap > best_gap:
                break  # sorted: gaps only grow from here
        if best is not None and best_gap <= tolerance_min:
            entry = {
                "valid_time": utc_iso(fdt),
                "model": f.get("model"),
                "obs_gap_min": round(best_gap, 2),
                "lead_hours": round((fdt - rdt).total_seconds() / 3600.0, 2) if rdt else None,
                "kind": "FORECAST_VS_OBSERVATION",
            }
            for k in ("temperature", "humidity", "pressure", "wind_speed", "precipitation",
                      "cloud_cover", "uv"):
                entry["fc_" + k] = f.get(k)
                entry["ob_" + k] = best.get(k)
            aligned.append(entry)
    return aligned
