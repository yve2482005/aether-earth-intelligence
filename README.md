# AETHER Earth Intelligence

Global weather + earthquake + tsunami + risk intelligence platform.
One codebase, honest by design: every datum is labeled as what it actually is
— **OBSERVATION, DETECTION, FORECAST, WARNING, RISK** — and the project never
claims certainty it does not have. See [HONESTY.md](HONESTY.md).

## What it does (and does not do)

- **Weather**: current/hourly/daily forecasts from free model APIs (Open-Meteo),
  stored in a local SQLite database, with per-task model skill metrics
  (MAE/RMSE/Brier/CRPS/calibration error).
- **Earthquakes**: real-time detection from USGS GeoJSON feeds and regional
  networks, with dedup, tracking, aftershock probability outlooks (labeled as
  probabilities, never predictions), ShakeMap/Felt-report integration.
- **Tsunami**: official warning messages only (e.g. NWS CAP), parsed with the
  original classification preserved; correlation to earthquakes says
  "potentially relevant" — never asserts a tsunami without an authority.
- **Risk**: historical/hazard statistics. No earthquake prediction exists in
  this system and none is planned.
- **What it does not do**: predict earthquakes, invent data, show fake
  metrics (missing data = honest "unavailable" state), or send learner/user
  data anywhere it was not asked to go.

## Project status (as of 2026-10-05, evening +0630)

All workstream code has landed:

| Area | Status |
|---|---|
| Build contracts (`CONTRACTS.md`) | Done |
| SQLite schema (`backend/db/schema.sql`) | Done (source of truth) |
| Backend: collectors, processing, alerting, confidence, API, scripts | Landed (backend team) |
| ML: weather models, leakage guards, backtest, seismic risk, registry | Landed (ML team) |
| Web PWA (`web/`, Burmese-first, 3 locales) | Landed, verified by web team (`web/VERIFICATION.txt`: 10/10 checks) |
| Android WebView shell (`app/`) | Landed; debug APK at `app/build/aether-debug.apk` (device testing pending) |
| Google Sheets / Drive integrations | Done (activate only with env creds) |
| CI workflows (`.github/workflows/`) | Done (9 files) |
| Docs (`docs/`) | Done (14 files) |
| Test suite (`tests/`) | Done — non-ML (this workstream) + ML (ML team) |

Remaining honest gaps: collectors have not yet run against live endpoints
(sample JSON for `docs/verification/` pending); no real-device testing yet;
no champion ML model trained/evaluated yet (`/v1/accuracy` has no real
numbers).

## Quick start

```bash
pip install -r requirements.txt        # stdlib + requests/fastapi/uvicorn/sklearn/numpy/joblib/gspread/google-api-python-client
python scripts/init_db.py             # creates data/aether.db from backend/db/schema.sql
python scripts/run_collect.py weather # collectors (backend team)
python scripts/run_api.py             # FastAPI on /v1 (backend team)
python -m pytest tests/ -q            # test suite
```

## Layout

```
backend/
  common/        config, http, logging, time utils
  db/            schema.sql (SOURCE OF TRUTH), sqlite wrapper
  collectors/    weather / earthquake / tsunami collectors
  processing/    validation, normalization, dedup, alignment
  alerting/      engine, dedup, severity
  confidence/    confidence engine
  integrations/  Google Sheets mirror + Drive archive (env-gated, this workstream)
  api/           FastAPI app, /v1 routes
  ml_support/    model registry (ML team)
ml/              weather models, seismic risk (NO prediction), calibration, backtest
web/             PWA (Burmese-first, my default)
app/             Android WebView shell
alerts/          thin re-exports of backend/alerting
gis/             offline gazetteer stub, geojson helpers
scripts/         init_db.py, run_collect.py, run_api.py, seed_registry.py
docs/            the 14 project docs
.github/workflows/  9 CI/scheduled workflows
```

## API (contract)

Base `/v1`: `/v1/weather/current|hourly|daily|map`, `/v1/earthquakes`,
`/v1/earthquakes/{id}`, `/v1/tsunami`, `/v1/alerts`, `/v1/events`,
`/v1/accuracy`, `/v1/models`, `/v1/locations`, `/v1/health`, plus
`/health`, `/status`, `/metrics`. Every user-visible response includes
`provenance`: `{sources, updated_at, data_age_min}`.

## Data sources (all free, zero spend)

Open-Meteo (no key, CC BY 4.0, fair use), USGS GeoJSON earthquake feeds
(US public domain), NWS CAP tsunami/alerts (US public domain, User-Agent
required). Full table with license terms: [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md),
[docs/DATA_LICENSES.md](docs/DATA_LICENSES.md).

## Google Sheets / Drive (optional mirrors)

Disabled by default. Set `AETHER_GOOGLE_CREDENTIALS_JSON` (service-account
JSON) plus `AETHER_SHEETS_SPREADSHEET_ID` / `AETHER_DRIVE_ROOT_FOLDER_ID` to
enable. Sheets is a control/metadata mirror only; Drive is an archive mirror
only; SQLite stays primary. Health endpoint reports `not_configured` when
unset. See [docs/OPERATIONS.md](docs/OPERATIONS.md).

## Honest limitations

- **No live verification yet**: collectors have landed but not yet run
  against live endpoints; `docs/verification/` will hold sample outputs
  once done.
- **No device testing**: the debug APK exists (`app/build/aether-debug.apk`)
  but has not been tested on a real Android device yet.
- **Tsunami fallback**: if the official feed is unreachable, the honest
  answer is "unavailable" — there is no invented substitute.
- **Satellite/radar**: availability is reported per source; coverage is not
  global and the app says so where it matters.
- **ML models**: no champion model exists yet; `/v1/accuracy` will show
  honest unavailable until the ML team lands evaluated models.
- **Zero spend constraint**: free tiers only (rate limits apply); no paid
  APIs, no cloud services.

## Docs

Start at [docs/README.md](docs/README.md).
