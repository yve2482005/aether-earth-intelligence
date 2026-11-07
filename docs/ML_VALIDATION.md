# ML Validation

This is the protocol the ML team must follow. It exists so that reported
skill numbers mean something — a model that "scores well" because it peeked
at the future is worse than useless; it is misleading.

## The time boundary

- Every dataset is split by **time**, never by random shuffle:
  `training_end <= validation_start <= test_start`.
- Features for a forecast issued at time T may only use data available at
  time T. Observation at T+1 used as a feature for a forecast at T is
  leakage, full stop.
- `ml/weather/dataset.py` builds datasets; `ml/weather/leakage.py` asserts
  the boundary holds. `train.py` refuses to run when leakage checks fail.

## Leakage tests (required CI pass)

`tests/test_ml_leakage.py` (ML team) must assert at least:

1. No timestamp in the training set is later than any timestamp in the
   validation set.
2. No feature column is derived from a target observed after forecast time.
3. Location-based splits (if used) do not leak through shared model runs.
4. Re-running dataset construction is deterministic (same rows, same splits).

Every GitHub workflow runs these as a required pass: if the leakage tests
fail, the workflow fails, and no model is trained or deployed.

## Backtesting

`ml/evaluation/backtest.py` walks forward through time: train on [start, t],
evaluate on (t, t+h], advance t, repeat. Metrics are aggregated over the
walk, so the reported MAE/RMSE/Brier/CRPS reflect how the model would have
performed in operations, not how it fits the past.

## Calibration

`ml/calibration/` holds shared calibration utilities; `ml/weather/calibrate.py`
applies them. A probability model is not done when its Brier score looks
good — it must also be calibrated (predicted 40% ≈ observed 40%), and the
calibration error is published in `model_scores` alongside everything else.

## Seismic risk is not prediction

`ml/risk/seismic_risk.py` computes **historical and hazard statistics**
(event rates, hazard curves from catalog data). It is validated like any
statistical estimator, and its outputs are labeled RISK — long-run hazard,
never "the next earthquake". Any attempt to add a predictive time/place/
magnitude output is out of scope and must be rejected in review.

## Champion / challenger

Recorded in `model_versions` / `training_runs` via
`backend/ml_support/registry.py`:

- A challenger becomes champion only by beating the champion on the held-out
  validation window on the task's primary metric, with calibration intact.
- Rejected runs stay in `training_runs` with status `rejected` and the reason
  in `notes`. History is not rewritten.

## Honest limitations

- Validation measures past performance on past data; it does not promise
  future performance. Model cards state this explicitly.
- Small sample counts (new locations, rare extremes) make metrics noisy;
  `sample_count` and the evaluation period are always published with the
  metric so readers can judge for themselves.
- As of 2026-10-05 the ML code has landed (including the leakage guards
  and backtest harness) but no champion model has been trained; this
  protocol is what the training runs must satisfy.
