# AETHER Build Contracts (read first — do not break)

## Root
`~/workspace/aether-earth-intelligence/`

## Layout (spec §28)
```
app/            -> Android WebView shell (native wrapper)
web/            -> PWA: index.html, css/, js/, i18n/, manifest.json, sw.js
backend/
    common/     -> config.py, http.py, logging_setup.py, timeutil.py
    db/         -> schema.sql (SOURCE OF TRUTH), db.py (sqlite wrapper)
    collectors/
        weather/    -> openmeteo.py (models: gfs, icon_seamless, ecmwf_ifs, jma_seamless), observations.py, satellite.py, radar.py
        earthquake/ -> usgs.py, regional_networks.py, event_normalizer.py, event_deduplicator.py, event_tracker.py, aftershock.py, shakemap.py, felt_reports.py
        tsunami/    -> warning_sources.py, message_parser.py, event_matcher.py, arrival.py, coastal_risk.py, observations.py
    processing/ -> validation.py, normalization.py, deduplication.py, alignment.py
    alerting/   -> engine.py, dedup.py, severity.py
    confidence/ -> engine.py
    integrations/ -> sheets.py, drive.py   (activate ONLY when creds configured; else report "not configured")
    api/        -> app.py (FastAPI), routes/*.py
    ml_support/ -> registry.py (model_versions/model_scores/training_runs read+write)  [owned by ML team]
collectors/     -> legacy symlink target; put ALL collectors under backend/collectors/
processing/     -> see backend/processing (single home)
ml/
    weather/    -> dataset.py, train.py, calibrate.py, evaluate.py, leakage.py
    risk/       -> seismic_risk.py (historical/hazard, NO prediction)
    calibration/-> shared calibration utils
    evaluation/ -> backtest.py
alerts/         -> re-exports from backend/alerting (thin __init__ only)
gis/            -> geocode.py (offline gazetteer stub), geojson.py helpers
tests/          -> pytest suite
scripts/        -> init_db.py, run_collect.py, run_api.py, seed_registry.py
docs/           -> the 14 required docs
.github/workflows/ -> 9 workflow files
```

## Database
- Primary: SQLite file `data/aether.db` (created by `scripts/init_db.py` from `backend/db/schema.sql`).
- `backend/db/schema.sql` is the SOURCE OF TRUTH. Do not invent new table names; extend only by agreement (append columns, never rename).
- SQLite `strftime` for times; store all timestamps as UTC ISO-8601 strings.
- Google Sheets/Drive are MIRRORS of summary data only (controlled by env flags), never the primary store.

## API contract (FastAPI, base `/v1`)
GET /v1/weather/current?lat=&lon= | /v1/weather/hourly | /v1/weather/daily | /v1/weather/map
GET /v1/earthquakes | /v1/earthquakes/{id} | /v1/tsunami | /v1/alerts | /v1/events
GET /v1/accuracy | /v1/models | /v1/locations | /v1/health
Plus `/health`, `/status`, `/metrics` (Prometheus-style text).
Every response object that is user-visible MUST include `provenance`: {sources:[], updated_at, data_age_min}.
Use `lat`/`lon` query params; resolve a saved location via `location_id`.

## i18n
- `web/i18n/{en,my,ja}.json`, flat keys. DEFAULT LANGUAGE: `my` (Burmese-first).
- Official warning categories (WARNING/ADVISORY/WATCH/INFORMATION/NO THREAT) are NEVER translated in meaning — display original + localized explanation side by side.

## Scientific honesty (hard, enforced by tests)
- FORECAST / DETECTION / OBSERVATION / WARNING / RISK / PREDICTION are distinct labels. API + UI must tag every datum with its kind.
- Never produce earthquake prediction (no future time/location/magnitude). Aftershock outlook = probability from official/statistical sources only, labeled "probability forecast, not a guarantee".
- Tsunami: only official messages; parser preserves `raw_classification`. Correlation may say "potentially relevant earthquake detected" — NEVER "tsunami will occur" without an authoritative warning.
- No "100% accurate" / "guaranteed" strings anywhere. Metrics: per-task MAE/RMSE/Brier/CRPS/calibration error only.
- Unavailable data: honest unavailable state with timestamp of last verified update (e.g. "Official tsunami data currently unavailable."). Satellite/radar: honestly report availability per source.
- tests must include assertions that ban the forbidden phrases in user-facing strings.

## Zero spend
No paid APIs. Free: Open-Meteo (no key), USGS GeoJSON feeds, NWS CAP tsunami (verify terms; fallback = honest-unavailable). All Python deps: stdlib + requests + fastapi + uvicorn + scikit-learn + numpy (pip install allowed, no paid services).

## Secrets
Env vars only (`AETHER_*`). Never commit secrets. Never print secret values.

## Ownership (who writes what — do not collide)
- Backend team: backend/{common,db,collectors,processing,alerting,confidence,api}, scripts/
- ML team: ml/, backend/ml_support/registry.py, tests/test_ml_*.py
- Web team: web/ (reads /v1 API), gis/
- Android team: app/
- Integration/docs team: backend/integrations/, .github/, docs/, tests/ (non-ML), README.md, HONESTY.md
- If two teams need the same new schema column, the BACKEND team owns schema.sql edits.

## Live verification
Collectors must be run at least once against the live endpoints and their output recorded (sample JSON in `docs/verification/` is allowed, no secrets). Pytest suite must pass. Android team must produce a debug APK artifact path.
