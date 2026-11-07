"""Normalization: provider payloads -> canonical AETHER fields.

Canonical units: temperature C, wind m/s, pressure hPa, precipitation mm,
probabilities/percentages 0..100, cloud cover %, UV index unitless, AQI US scale.
Conversions are defensive: they only apply when the source unit is declared.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from backend.common.timeutil import parse_iso, utc_iso


def kmh_to_ms(kmh: Optional[float]) -> Optional[float]:
    return None if kmh is None else kmh / 3.6


def ms_to_kmh(ms: Optional[float]) -> Optional[float]:
    return None if ms is None else ms * 3.6


OPENMETEO_HOURLY_MAP = {
    "temperature_2m": "temperature",
    "relative_humidity_2m": "humidity",
    "pressure_msl": "pressure",
    "wind_speed_10m": "wind_speed",
    "wind_direction_10m": "wind_direction",
    "precipitation": "precipitation",
    "precipitation_probability": "precip_probability",
    "cloud_cover": "cloud_cover",
    "uv_index": "uv",
    "weather_code": "weather_code",
    "visibility": "visibility",
    "dew_point_2m": "dew_point",
    "wind_gusts_10m": "wind_gust",
}

OPENMETEO_CURRENT_MAP = {
    "temperature_2m": "temperature",
    "relative_humidity_2m": "humidity",
    "apparent_temperature": "feels_like",
    "pressure_msl": "pressure",
    "wind_speed_10m": "wind_speed",
    "wind_direction_10m": "wind_direction",
    "precipitation": "precipitation",
    "weather_code": "weather_code",
    "cloud_cover": "cloud_cover",
}


def normalize_openmeteo_hourly(payload: Dict[str, Any], model: str,
                                wind_unit: str = "ms") -> list[Dict[str, Any]]:
    """Flatten one Open-Meteo hourly payload into canonical rows."""
    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []
    rows: list[Dict[str, Any]] = []
    for i, t in enumerate(times):
        row: Dict[str, Any] = {
            "source": "open-meteo",
            "model": model,
            "valid_time": utc_iso(parse_iso(t)),
        }
        for api_key, canon in OPENMETEO_HOURLY_MAP.items():
            vals = hourly.get(api_key)
            v = vals[i] if vals and i < len(vals) else None
            if api_key == "wind_speed_10m" and v is not None and wind_unit == "kmh":
                v = kmh_to_ms(v)
            row[canon] = v
        rows.append(row)
    return rows


def normalize_openmeteo_current(payload: Dict[str, Any], wind_unit: str = "ms") -> Dict[str, Any]:
    cur = payload.get("current") or {}
    row: Dict[str, Any] = {"valid_time": utc_iso(parse_iso(cur.get("time")))}
    for api_key, canon in OPENMETEO_CURRENT_MAP.items():
        v = cur.get(api_key)
        if api_key == "wind_speed_10m" and v is not None and wind_unit == "kmh":
            v = kmh_to_ms(v)
        row[canon] = v
    return row


def normalize_openmeteo_daily(payload: Dict[str, Any], model: str) -> list[Dict[str, Any]]:
    daily = payload.get("daily") or {}
    times = daily.get("time") or []
    rows: list[Dict[str, Any]] = []
    for i, t in enumerate(times):
        row = {"source": "open-meteo", "model": model, "date": str(t)}
        for api_key in ("temperature_2m_max", "temperature_2m_min", "precipitation_sum",
                        "precipitation_probability_max", "wind_speed_10m_max", "uv_index_max",
                        "weather_code"):
            vals = daily.get(api_key)
            row[api_key] = vals[i] if vals and i < len(vals) else None
        rows.append(row)
    return rows
