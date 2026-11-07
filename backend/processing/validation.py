"""Validation: schema/unit/range checks for incoming data.

Rules:
- Missing (None) values are KEPT as None and reported as errors; they are NEVER fabricated.
- Out-of-range values are kept but flagged 'suspect' in the errors list; callers decide to drop.
Returns (cleaned_row_dict, errors_list).
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

# Physical plausibility ranges (canonical units).
RANGES: Dict[str, Tuple[float, float]] = {
    "temperature": (-90.0, 62.0),       # C
    "feels_like": (-90.0, 70.0),        # C
    "humidity": (0.0, 100.0),           # %
    "pressure": (870.0, 1100.0),        # hPa
    "wind_speed": (0.0, 150.0),         # m/s
    "wind_direction": (0.0, 360.0),     # deg
    "precipitation": (0.0, 500.0),      # mm
    "precip_probability": (0.0, 100.0), # %
    "cloud_cover": (0.0, 100.0),        # %
    "uv": (0.0, 20.0),                 # UV index
    "aqi": (0.0, 500.0),               # US AQI
    "dew_point": (-90.0, 62.0),        # C
    "wind_gust": (0.0, 150.0),         # m/s
    "visibility": (0.0, 100000.0),     # m
    "latitude": (-90.0, 90.0),
    "longitude": (-180.0, 180.0),
    "magnitude": (0.0, 10.5),
    "depth": (0.0, 750.0),             # km
}


def _check(name: str, value: Any, errors: List[str]) -> Any:
    if value is None:
        errors.append(f"missing:{name}")
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        errors.append(f"non-numeric:{name}={value!r}")
        return None
    lo, hi = RANGES.get(name, (float("-inf"), float("inf")))
    if not (lo <= v <= hi):
        errors.append(f"out-of-range:{name}={v} (expected {lo}..{hi})")
    return v


def validate_forecast_row(row: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    errors: List[str] = []
    cleaned = dict(row)
    for name in ("temperature", "feels_like", "humidity", "pressure", "wind_speed",
                 "wind_direction", "precipitation", "precip_probability",
                 "cloud_cover", "uv", "aqi", "visibility", "dew_point", "wind_gust"):
        if name in cleaned:
            cleaned[name] = _check(name, cleaned.get(name), errors)
    if not cleaned.get("valid_time"):
        errors.append("missing:valid_time")
    return cleaned, errors


def validate_observation_row(row: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    errors: List[str] = []
    cleaned = dict(row)
    for name in ("temperature", "humidity", "pressure", "wind_speed",
                 "wind_direction", "precipitation"):
        if name in cleaned:
            cleaned[name] = _check(name, cleaned.get(name), errors)
    if not cleaned.get("observed_at"):
        errors.append("missing:observed_at")
    return cleaned, errors


def validate_earthquake_row(row: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    errors: List[str] = []
    cleaned = dict(row)
    for name in ("latitude", "longitude", "depth", "magnitude"):
        cleaned[name] = _check(name, cleaned.get(name), errors)
    if cleaned.get("latitude") is None or cleaned.get("longitude") is None:
        errors.append("invalid:coordinates-missing")
    if not cleaned.get("origin_time"):
        errors.append("missing:origin_time")
    return cleaned, errors
