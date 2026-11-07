# Data Sources

All sources are free and require no API key (zero-spend rule). License terms
live in [DATA_LICENSES.md](DATA_LICENSES.md); the `source_registry` table
(`backend/db/schema.sql`) is the runtime record of each source's health and
metadata.

## Weather

| Source | What | Endpoint family | Cadence | Auth |
|---|---|---|---|---|
| Open-Meteo forecast | Current/hourly/daily, 16-day, 50+ vars | `api.open-meteo.com/v1/forecast` | 30 min poll | none (fair use 10k/day) |
| Open-Meteo model endpoints | Per-model runs: `gfs`, `icon_seamless`, `ecmwf_ifs`, `jma_seamless` | `/v1/{gfs,ecmwf,dwd-icon,...}` | 30 min poll | none |
| Open-Meteo archive | ERA5 reanalysis 1940–present (training) | `/v1/archive` | on demand | none |
| Open-Meteo historical forecast | Past forecasts as issued (skill scoring) | historical forecast API | on demand | none |
| Open-Meteo air quality | PM2.5/PM10/O₃/NO₂/SO₂/CO, AQI | `/v1/air-quality` | 30 min poll | none |

Collectors live at `backend/collectors/weather/` (openmeteo.py,
observations.py, satellite.py, radar.py — backend team).

## Earthquake

| Source | What | Format | Cadence | Auth |
|---|---|---|---|---|
| USGS earthquake feeds | Global detections: magnitude, location, depth, time | GeoJSON (`earthquake.usgs.gov` feeds) | 15 min poll | none |
| Regional networks | Supplementary detections | GeoJSON/various | 15 min poll | none |
| USGS ShakeMap | Shaking intensity maps | product links | on detection | none |
| USGS "Did You Feel It" | Felt reports | product links | on detection | none |

Normalizer + dedup at `backend/collectors/earthquake/` (event_normalizer.py,
event_deduplicator.py, event_tracker.py, aftershock.py, shakemap.py,
felt_reports.py — backend team).

## Tsunami

| Source | What | Format | Cadence | Auth |
|---|---|---|---|---|
| NWS CAP tsunami products | Official US tsunami messages | CAP/XML via `api.weather.gov` | 10 min poll | none, **identifying User-Agent required** |

Fallback when unreachable: honest "unavailable" state — there is no
invented substitute. Parser at `backend/collectors/tsunami/`
(warning_sources.py, message_parser.py, event_matcher.py, arrival.py,
coastal_risk.py, observations.py — backend team).

## Satellite / radar

Collectors are stubbed at `backend/collectors/weather/satellite.py` and
`radar.py`. Availability is reported **per source** in the UI/API; the
project makes no global-coverage claims. These layers are honest about what
they can and cannot show for a given region.

## Source health

Each poll writes to `system_health` (`component = "collector:<source_id>"`,
status ok/degraded/down/unavailable/unknown, latency, error rate, record
counts). `/v1/health` exposes it. Sheets mirror tab `source_health` carries
the same data when the mirror is enabled.

## Honest limitations

- The collector modules have landed but had not yet run against live
  endpoints when this doc was written; endpoint paths above are the intended
  ones from the spec and must be confirmed by the backend team's first live
  run (sample JSON allowed in `docs/verification/`).
- Free-tier rate limits apply (Open-Meteo ~10k/day fair use; NWS generous but
  unpublished). The GitHub Actions schedules are deliberately conservative.
- Tsunami coverage depends on the official feed; outside its area of
  responsibility the honest answer is "no official data", not silence and
  not invention.
- Satellite/radar are availability-per-source; do not present them as global.
