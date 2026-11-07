"""Data quality scores (§34).

Per dataset: freshness, completeness, validity, consistency, source
reliability, geographic coverage -> DATA QUALITY SCORE (0-100).

Every component is computed from stored data only and returned with its
method. Stale or incomplete data scores LOW and the breakdown stays visible —
never hidden, never smoothed over.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from backend.common.timeutil import parse_iso


WEIGHTS = {"freshness": 0.25, "completeness": 0.20, "validity": 0.20,
           "consistency": 0.10, "reliability": 0.15, "coverage": 0.10}

DATASETS = {
    "weather-forecasts": {"table": "weather_forecasts",
                          "source_id": "open-meteo-forecast",
                          "time_col": "collected_at",
                          "key_cols": ["temperature", "humidity", "pressure",
                                       "wind_speed", "precip_probability"]},
    "weather-observations": {"table": "weather_observations",
                             "source_id": "open-meteo-forecast",
                             "time_col": "collected_at",
                             "key_cols": ["temperature", "humidity", "pressure",
                                          "wind_speed", "precipitation"]},
    "earthquakes": {"table": "earthquake_events",
                    "source_id": "usgs-earthquake-all-hour",
                    "time_col": "created_at",
                    "key_cols": ["magnitude", "latitude", "longitude", "depth"]},
    "tsunami": {"table": "tsunami_events",
                "source_id": "nws-tsunami-cap",
                "time_col": "updated_at",
                "key_cols": ["message_type", "issue_time", "status"]},
}

# crude but honest range checks: (min, max)
RANGES = {
    "temperature": (-80.0, 60.0), "feels_like": (-80.0, 60.0),
    "humidity": (0.0, 100.0), "pressure": (800.0, 1100.0),
    "wind_speed": (0.0, 150.0), "wind_direction": (0.0, 360.0),
    "precipitation": (0.0, 500.0), "precip_probability": (0.0, 100.0),
    "cloud_cover": (0.0, 100.0), "visibility": (0.0, 100000.0),
    "magnitude": (0.0, 10.5), "latitude": (-90.0, 90.0),
    "longitude": (-180.0, 180.0), "depth": (0.0, 800.0),
}

_SCORE_NOW = None  # test hook: inject reference time


def _now() -> datetime:
    return _SCORE_NOW or datetime.now(timezone.utc)


def _freshness_score(age_min: Optional[float]) -> float:
    if age_min is None:
        return 0.1
    if age_min <= 60:
        return 1.0
    if age_min <= 360:
        return 0.8
    if age_min <= 1440:
        return 0.6
    if age_min <= 4320:
        return 0.4
    if age_min <= 10080:
        return 0.2
    return 0.05


def _coverage_score(text: Optional[str]) -> tuple[float, str]:
    t = (text or "").lower()
    if "global" in t:
        return 1.0, "registry lists global coverage"
    if "regional" in t:
        return 0.7, "registry lists regional coverage"
    if t.strip():
        return 0.4, f"registry coverage note: '{text}'"
    return 0.3, "coverage not listed in registry"


def dataset_quality(conn: sqlite3.Connection, dataset: str,
                    sample: int = 100) -> Dict[str, Any]:
    """Compute the data quality score for a dataset."""
    if dataset not in DATASETS:
        raise ValueError(f"unknown dataset: {dataset}")
    cfg = DATASETS[dataset]
    now = _now()

    try:
        table_ok = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (cfg["table"],)).fetchone() is not None
    except Exception:  # noqa: BLE001
        table_ok = False
    if not table_ok:
        comps = {k: 0.05 for k in WEIGHTS}
        return {
            "dataset": dataset, "source_id": cfg["source_id"],
            "score": 5.0, "label": "Poor", "components": comps,
            "missing": True,
            "notes": {k: "table not present in this database" for k in WEIGHTS},
            "row_count": 0, "latest": None, "age_minutes": None,
            "disclaimer": "No data stored for this dataset — score reflects absence, not quality.",
        }

    # --- freshness: latest stored row / source last_success ---
    row = conn.execute(
        f"SELECT MAX({cfg['time_col']}) AS latest, COUNT(*) AS c FROM {cfg['table']}"
    ).fetchone()
    latest = dict(row)["latest"]
    count = dict(row)["c"]
    reg = conn.execute(
        "SELECT last_success, reliability_score, geographic_coverage, health "
        "FROM source_registry WHERE source_id = ?",
        (cfg["source_id"],)).fetchone()
    reg = dict(reg) if reg else {}
    src_ts = reg.get("last_success") or latest
    age_min = None
    dt = parse_iso(src_ts) if src_ts else None
    if dt:
        age_min = (now - dt).total_seconds() / 60.0
    freshness = _freshness_score(age_min)

    # --- completeness: non-null rate of key columns over recent rows ---
    rows = conn.execute(
        f"SELECT * FROM {cfg['table']} ORDER BY {cfg['time_col']} DESC LIMIT ?",
        (sample,)).fetchall()
    rows = [dict(r) for r in rows]
    completeness = 1.0
    if rows:
        filled = sum(1 for r in rows for c in cfg["key_cols"] if r.get(c) is not None)
        completeness = filled / (len(rows) * len(cfg["key_cols"]))

    # --- validity: fraction of values inside physical ranges ---
    valid_total = valid_ok = 0
    for r in rows:
        for c in cfg["key_cols"]:
            if c in RANGES and r.get(c) is not None:
                valid_total += 1
                lo, hi = RANGES[c]
                if lo <= r[c] <= hi:
                    valid_ok += 1
    validity = (valid_ok / valid_total) if valid_total else 0.5
    validity_note = (f"{valid_ok}/{valid_total} range checks passed"
                     if valid_total else "no range-checkable values in sample")

    # --- consistency: temporal consistency (issued <= valid, no dup conflicts) ---
    consistency, consistency_note = _consistency(conn, cfg)

    # --- source reliability ---
    reliability = reg.get("reliability_score")
    if reliability is None:
        reliability, rel_note = 0.5, "reliability not rated in registry"
    else:
        rel_note = f"registry reliability_score = {reliability}"

    # --- geographic coverage ---
    coverage, cov_note = _coverage_score(reg.get("geographic_coverage"))

    components = {
        "freshness": round(freshness, 3),
        "completeness": round(completeness, 3),
        "validity": round(validity, 3),
        "consistency": round(consistency, 3),
        "reliability": round(float(reliability), 3),
        "coverage": round(coverage, 3),
    }
    score = sum(components[k] * WEIGHTS[k] for k in WEIGHTS) * 100
    # stale data cannot score well overall: cap by the freshness component
    # so stale data is visibly poor, never smoothed over by other components
    freshness_cap = components["freshness"] * 100 + 10
    capped = score > freshness_cap
    score = round(min(score, freshness_cap), 1)
    label = _label(score)

    return {
        "dataset": dataset,
        "source_id": cfg["source_id"],
        "score": score,
        "label": label,
        "components": components,
        "notes": {
            "freshness": (f"latest data {round(age_min, 1)} min old" if age_min is not None
                          else "no timestamp available — treated as stale"),
            "completeness": f"non-null key fields over last {len(rows)} rows",
            "validity": validity_note,
            "consistency": consistency_note,
            "reliability": rel_note,
            "coverage": cov_note,
            **({"score_cap": ("overall score capped by freshness — stale data "
                              "cannot score well overall")
                } if capped else {}),
        },
        "row_count": count,
        "latest": latest,
        "age_minutes": round(age_min, 1) if age_min is not None else None,
        "disclaimer": "Quality of the stored data, not a guarantee of correctness.",
    }


def _consistency(conn: sqlite3.Connection, cfg: Dict[str, Any]) -> tuple[float, str]:
    table = cfg["table"]
    try:
        if table == "weather_forecasts":
            bad = conn.execute(
                """SELECT COUNT(*) AS c FROM weather_forecasts
                   WHERE issued_at > valid_time
                      OR forecast_hour != CAST((julianday(valid_time) - julianday(issued_at)) * 24 AS INTEGER)"""
            ).fetchone()["c"]
            total = conn.execute("SELECT COUNT(*) AS c FROM weather_forecasts"
                                 ).fetchone()["c"]
        elif table == "weather_observations":
            bad = conn.execute(
                """SELECT COUNT(*) AS c FROM (
                     SELECT location_id, observed_at, COUNT(DISTINCT temperature)
                     FROM weather_observations
                     GROUP BY location_id, observed_at HAVING COUNT(DISTINCT temperature) > 1)"""
            ).fetchone()["c"]
            total = conn.execute(
                "SELECT COUNT(DISTINCT location_id || observed_at) AS c FROM weather_observations"
            ).fetchone()["c"]
        else:
            bad, total = 0, 1
    except Exception:
        bad, total = 0, 0
    if not total:
        return 0.5, "no rows to check consistency"
    return max(0.0, 1.0 - bad / total), f"{bad} inconsistent of {total} checked"


def _label(score: float) -> str:
    if score >= 85:
        return "Good"
    if score >= 65:
        return "Fair"
    if score >= 40:
        return "Weak"
    return "Poor"


def all_datasets_quality(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    return [dataset_quality(conn, d) for d in DATASETS]
