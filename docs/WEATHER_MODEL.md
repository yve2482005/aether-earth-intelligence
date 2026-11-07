# Weather Model System

## Pipeline

```
Open-Meteo (gfs · icon_seamless · ecmwf_ifs · jma_seamless)
        │  backend/collectors/weather/openmeteo.py  (backend team)
        ▼
weather_forecasts  (per location × model × run × hour)
        │  backend/processing/  (validate, normalize, align)
        ▼
ml/weather/  dataset.py → train.py → calibrate.py → evaluate.py  (ML team)
        │  scores → backend/ml_support/registry.py
        ▼
model_versions · model_scores · training_runs   →  /v1/accuracy, /v1/models
```

## What the models do

Per-task models: temperature, rain probability, precipitation amount, wind,
pressure, humidity. Training features come from the multi-model forecast
archive (Open-Meteo archive = ERA5 reanalysis; historical-forecast API = past
forecasts as issued, so skill is measured against what was actually
forecast, not reanalysis).

Calibration (`calibrate.py`) adjusts predicted probabilities so a "40%"
means roughly 40% in the long run; calibration error is reported as a
metric, not hidden.

## Metrics (the only language allowed for skill)

Per task, horizon, location, model, and model version — stored in
`model_scores`:

| Metric | For |
|---|---|
| MAE / RMSE | temperature, wind, pressure, humidity, precip amount |
| Brier score | rain probability |
| CRPS | probabilistic forecasts |
| calibration error | probability reliability |
| sample_count, period_start/end | so every number has a denominator |

There is no single "accuracy %" anywhere. An unevaluated model shows
honest "unavailable" in `/v1/accuracy`, never 0 and never a placeholder.

## Leakage prevention (summary)

Full protocol in [ML_VALIDATION.md](ML_VALIDATION.md). The non-negotiables:

- Training windows end strictly before validation windows (no peeking).
- Features use only data available at forecast time (no future observations).
- `ml/weather/leakage.py` contains automated checks; `train.py` refuses to
  run when they fail; CI runs the leakage tests as a required pass.
- Backtesting (`ml/evaluation/backtest.py`) walks forward in time — no
  shuffling across the time boundary.

## Champion model process

`model_versions.is_champion` marks the deployed model per task. A challenger
replaces the champion only when it beats it on the held-out validation
window on the task's primary metric **and** passes calibration checks. Every
run is recorded in `training_runs` (status success/failed/rejected, metrics
JSON, git commit).

## Model cards

Every champion gets a model card per [MODEL_CARD.md](MODEL_CARD.md):
intended use, training data, metrics, limitations, and the honest statement
of what it cannot do.

## Honest limitations

- As of 2026-10-05 no model has been trained or evaluated; `/v1/accuracy`
  responds but has no real numbers yet and says so honestly.
- Free data sources bound the skill ceiling: Open-Meteo aggregates public
  models; AETHER does not run its own numerical weather prediction.
- Forecasts degrade with horizon; the UI must show uncertainty growing with
  lead time, not a single confident number at day 10.
- Observations used for verification come from the same free sources; gaps
  in station coverage mean some locations have weaker verification.
