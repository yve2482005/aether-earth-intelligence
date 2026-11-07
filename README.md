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

All workstream code has landed. See CONTRACTS.md, docs/README.md, and
HONESTY.md for the full picture. Remaining honest gaps: collectors have not
yet run against live endpoints; no real-device testing yet; no champion ML
model trained/evaluated yet.

## Quick start

```bash
pip install -r requirements.txt
python scripts/init_db.py
python scripts/run_collect.py weather
python scripts/run_api.py
python -m pytest tests/ -q
```

## Docs

Start at [docs/README.md](docs/README.md).
