#!/usr/bin/env python3
"""Seed source_registry with REAL, verified sources.

Honesty rule: every field is either verified from the provider's published
terms/docs or explicitly marked as assumed/unverified in `notes`. Nothing is
invented to look complete.
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from backend.db import db as dbmod  # noqa: E402

NOW = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

SOURCES = [
    {
        "source_id": "open-meteo-forecast",
        "provider": "Open-Meteo",
        "dataset": "NWP forecast API (GFS/ICON/ECMWF-IFS/JMA via models param)",
        "category": "weather",
        "url": "https://open-meteo.com/en/docs",
        "api_type": "REST JSON, no key",
        "update_frequency": "model runs every 2-6h depending on model",
        "geographic_coverage": "global",
        "resolution": "varies by model (e.g. GFS ~13km, ICON ~11km)",
        "variables": "temperature_2m, relative_humidity_2m, pressure_msl, wind_speed_10m, precipitation, precipitation_probability, cloud_cover, weather_code, uv_index",
        "license": "CC BY 4.0 (non-commercial use free; commercial use requires their paid/commercial terms)",
        "attribution": "Weather data by Open-Meteo.com",
        "commercial_use": "NOT permitted under free tier per Open-Meteo terms — verify https://open-meteo.com/en/terms before any commercial use",
        "redistribution": "per Open-Meteo terms (attribution required)",
        "rate_limits": "10,000 API calls/day on free tier (documented by Open-Meteo)",
        "auth_required": 0,
        "priority": 1,
        "reliability_score": 0.9,
        "health": "unknown",
        "notes": "Verified: no-key REST access, CC-BY-4.0 free tier. Non-commercial use only per terms. forecast_run not exposed by API — recorded as collection hour (documented).",
    },
    {
        "source_id": "open-meteo-air-quality",
        "provider": "Open-Meteo / CAMS",
        "dataset": "Air quality API (CAMS European model)",
        "category": "air_quality",
        "url": "https://open-meteo.com/en/docs/air-quality-api",
        "api_type": "REST JSON, no key",
        "update_frequency": "twice daily (CAMS)",
        "geographic_coverage": "global (CAMS)",
        "resolution": "~40km (CAMS)",
        "variables": "us_aqi, pm2_5, pm10, ozone, nitrogen_dioxide",
        "license": "CC BY 4.0 (same Open-Meteo terms); CAMS data © ECMWF/Copernicus",
        "attribution": "Air quality data: Open-Meteo.com / CAMS (Copernicus Atmosphere Monitoring Service)",
        "commercial_use": "NOT permitted under free tier — verify terms",
        "redistribution": "per terms (attribution required)",
        "rate_limits": "10,000 API calls/day on free tier",
        "auth_required": 0,
        "priority": 3,
        "reliability_score": 0.8,
        "health": "unknown",
        "notes": "Verified endpoint exists (air-quality-api.open-meteo.com). CAMS attribution required; commercial use per Open-Meteo terms.",
    },
    {
        "source_id": "usgs-earthquake-all-hour",
        "provider": "U.S. Geological Survey (USGS)",
        "dataset": "Earthquake GeoJSON feed — all_hour (M1+ past hour)",
        "category": "earthquake",
        "url": "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_hour.geojson",
        "api_type": "REST GeoJSON, no key",
        "update_frequency": "every minute",
        "geographic_coverage": "global",
        "resolution": "event-level",
        "variables": "origin_time, lat, lon, depth, mag, magType, place, felt, cdi, alert, tsunami, status",
        "license": "Public domain (U.S. federal government work)",
        "attribution": "U.S. Geological Survey",
        "commercial_use": "permitted (public domain); cite USGS",
        "redistribution": "permitted (public domain)",
        "rate_limits": "no published hard limit; USGS asks for polite, non-aggressive polling",
        "auth_required": 0,
        "priority": 1,
        "reliability_score": 0.95,
        "health": "unknown",
        "notes": "Verified live feed URL. Summary feed carries no ShakeMap grids — full products need ComCat detail fetch per event.",
    },
    {
        "source_id": "usgs-earthquake-significant-month",
        "provider": "U.S. Geological Survey (USGS)",
        "dataset": "Earthquake GeoJSON feed — significant_month",
        "category": "earthquake",
        "url": "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/significant_month.geojson",
        "api_type": "REST GeoJSON, no key",
        "update_frequency": "every minute",
        "geographic_coverage": "global",
        "resolution": "event-level",
        "variables": "same as all_hour",
        "license": "Public domain (U.S. federal government work)",
        "attribution": "U.S. Geological Survey",
        "commercial_use": "permitted (public domain); cite USGS",
        "redistribution": "permitted (public domain)",
        "rate_limits": "polite polling requested",
        "auth_required": 0,
        "priority": 1,
        "reliability_score": 0.95,
        "health": "unknown",
        "notes": "Verified live feed URL. Context feed for significant events of the past 30 days.",
    },
    {
        "source_id": "nws-tsunami-cap",
        "provider": "U.S. National Weather Service",
        "dataset": "NWS API active alerts (filtered for tsunami products)",
        "category": "tsunami",
        "url": "https://api.weather.gov/alerts/active?event=Tsunami%20Warning",
        "api_type": "GeoJSON API, no key (User-Agent required)",
        "update_frequency": "continuous (as issued)",
        "geographic_coverage": "U.S. + U.S. territories (tsunami products cover Pacific)",
        "resolution": "message-level",
        "variables": "message classification, severity, areas, issue/expire times, raw text",
        "license": "Public domain (U.S. federal government work) — ASSUMED, verify terms",
        "attribution": "U.S. National Weather Service",
        "commercial_use": "assumed permitted (federal public domain) — VERIFY TERMS before production use",
        "redistribution": "assumed permitted — VERIFY TERMS",
        "rate_limits": "unknown — poll no more often than every 5 minutes",
        "auth_required": 0,
        "priority": 1,
        "reliability_score": None,
        "health": "unknown",
        "notes": "Primary source migrated 2026-10-07: legacy alerts.weather.gov CAP atom feed failed persistently since ~2026-10-05 (empty reply); api.weather.gov verified live 2026-10-07 (HTTP 200, GeoJSON, 0 active tsunami alerts). License: NWS data is U.S. federal public domain; API use is free without key but requires a User-Agent (make_session already sends one).",
    },
    {
        "source_id": "satellite-imagery",
        "provider": "none (planned)",
        "dataset": "satellite imagery tiles",
        "category": "satellite",
        "url": None,
        "api_type": None,
        "update_frequency": None,
        "geographic_coverage": None,
        "resolution": None,
        "variables": None,
        "license": None,
        "attribution": None,
        "commercial_use": None,
        "redistribution": None,
        "rate_limits": None,
        "auth_required": 0,
        "priority": 5,
        "reliability_score": None,
        "health": "unavailable",
        "notes": "No free legal satellite tile source wired yet. API returns honest 'unavailable' state.",
    },
    {
        "source_id": "radar-imagery",
        "provider": "none (planned)",
        "dataset": "weather radar tiles",
        "category": "radar",
        "url": None,
        "api_type": None,
        "update_frequency": None,
        "geographic_coverage": None,
        "resolution": None,
        "variables": None,
        "license": None,
        "attribution": None,
        "commercial_use": None,
        "redistribution": None,
        "rate_limits": None,
        "auth_required": 0,
        "priority": 5,
        "reliability_score": None,
        "health": "unavailable",
        "notes": "No free legal radar tile source wired yet. API returns honest 'unavailable' state.",
    },
    {
        "source_id": "regional-seismic-networks",
        "provider": "none (planned)",
        "dataset": "regional seismic network feeds",
        "category": "earthquake",
        "url": None,
        "api_type": None,
        "update_frequency": None,
        "geographic_coverage": None,
        "resolution": None,
        "variables": None,
        "license": None,
        "attribution": None,
        "commercial_use": None,
        "redistribution": None,
        "rate_limits": None,
        "auth_required": 0,
        "priority": 4,
        "reliability_score": None,
        "health": "unavailable",
        "notes": "Not wired. USGS is the only live earthquake source; merge interface (spatial+temporal) is ready for future sources.",
    },
    {
        "source_id": "tsunami-sea-level",
        "provider": "none (planned)",
        "dataset": "sea-level / tide-gauge observations",
        "category": "tsunami",
        "url": None,
        "api_type": None,
        "update_frequency": None,
        "geographic_coverage": None,
        "resolution": None,
        "variables": None,
        "license": None,
        "attribution": None,
        "commercial_use": None,
        "redistribution": None,
        "rate_limits": None,
        "auth_required": 0,
        "priority": 4,
        "reliability_score": None,
        "health": "unavailable",
        "notes": "No free legal sea-level observation feed wired yet. Tsunami state comes from USGS tsunami_flag + official NWS CAP messages only.",
    },
]

COLUMNS = ("source_id provider dataset category url api_type update_frequency geographic_coverage "
           "resolution variables license attribution commercial_use redistribution rate_limits "
           "auth_required priority reliability_score health notes").split()


def main() -> None:
    conn = dbmod.connect()
    n = 0
    for s in SOURCES:
        conn.execute(
            f"""INSERT INTO source_registry ({','.join(COLUMNS)})
                VALUES ({','.join('?' * len(COLUMNS))})
                ON CONFLICT(source_id) DO UPDATE SET
                  provider=excluded.provider, dataset=excluded.dataset, category=excluded.category,
                  url=excluded.url, api_type=excluded.api_type,
                  update_frequency=excluded.update_frequency,
                  geographic_coverage=excluded.geographic_coverage, resolution=excluded.resolution,
                  variables=excluded.variables, license=excluded.license,
                  attribution=excluded.attribution, commercial_use=excluded.commercial_use,
                  redistribution=excluded.redistribution, rate_limits=excluded.rate_limits,
                  auth_required=excluded.auth_required, priority=excluded.priority,
                  reliability_score=excluded.reliability_score, notes=excluded.notes""",
            tuple(s.get(c) for c in COLUMNS))
        n += 1
    conn.commit()
    count = dbmod.table_count(conn, "source_registry")
    conn.close()
    print(f"seeded {n} source_registry entries (total rows: {count})")


if __name__ == "__main__":
    main()
