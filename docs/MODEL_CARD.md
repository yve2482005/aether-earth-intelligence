# Model Card

Every champion model (`model_versions.is_champion = 1`) ships with a card
filled from this template. A card is a user-facing honesty document: it says
what the model is for, what it was measured on, and what it cannot do. No
card, no deployment.

## Template

```markdown
# Model card: <version>  (e.g. weather_temperature_v2026_10_05_001)

## Intended use
Task: <temperature | rain_prob | precip_amount | wind | pressure | humidity>
Intended users: AETHER app users checking <task> forecasts.
Out-of-scope uses: <e.g. not for aviation decisions, not for emergency planning>

## Training data
Dataset version: <dataset_version> · Feature version: <feature_version>
Source versions: <source_versions>
Period: <training_start> to <training_end>
Rows: <rows> · Locations: <n>

## Evaluation
Validation period: <validation_start> to <validation_end>
Primary metric: <metric> = <value>  (sample_count=<n>)
Other metrics: MAE=… RMSE=… Brier=… CRPS=… calibration_error=…
Backtest: <walk-forward summary>

## Limitations
- <where it is weak: e.g. extremes, specific regions, long horizons>
- <known failure modes>
- Past performance does not promise future performance.

## Provenance
Git commit: <git_commit> · Training run: <run_id> · Deployed: <deployed_at>
```

## Process

1. `train.py` finishes → metrics written to `training_runs` (status
   success/failed/rejected).
2. `evaluate.py` + `backtest.py` produce validation numbers → proposed card
   filled from the template above, values copied from the registry (never
   hand-typed).
3. Human review: check the Limitations section is specific (not "may have
   errors" boilerplate), metrics have denominators, and no banned certainty
   phrase appears.
4. Champion flag set in `model_versions`; card stored with the release notes
   and linked from `/v1/models`.

## What a good Limitations section looks like

Bad: "The model may occasionally be wrong."
Good: "RMSE grows from 1.1 C at 24 h to 2.4 C at 120 h on the validation
window; rain-probability calibration error is 0.06 above 70% predicted;
performance at coastal mountain locations (sample_count < 200) is not
established."

## Honest limitations

- A model card describes the model as measured. It is not a warranty.
- Cards must be regenerated on every champion change; a stale card is a
  dishonest card. The registry's `deployed_at` is the freshness signal.
- As of 2026-10-05 no model cards exist because no models have been trained.
