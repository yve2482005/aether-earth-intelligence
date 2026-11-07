"""Per-task training for AETHER weather post-processing models.

Tasks: temperature, rain_probability, precip_amount, wind, pressure, humidity.
Algorithm: HistGradientBoosting (regressor / classifier), NaN-tolerant.

Pipeline per task:
  1. build_dataset()          -- forecast<->observation pairs + leakage guards
  2. strict time split       -- train < validation cutoff (purity asserted)
  3. location-aware fit      -- per location_id when >=200 train rows,
                               else one global fallback model
  4. rain_probability: per-regime Isotonic/Platt calibration on validation
  5. evaluate on validation  -- MAE/RMSE/CRPS or Brier/ECE, sliced by
                               horizon / location / regime -> model_scores
  6. persist artifacts to models/<version>/ (joblib + manifest + MODEL_CARD)
  7. registry: model_versions row, training_runs row, deploy gate vs champion

Synthetic smoke-test runs are labeled in training_runs.notes and are never
deployed and never marked champion.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.ml_support import registry  # noqa: E402
from ml.weather import calibrate as calib_mod  # noqa: E402
from ml.weather.dataset import (  # noqa: E402
    REGIMES, TASKS, build_dataset, epoch_to_iso, regime_names)
from ml.weather.evaluate import evaluate_task  # noqa: E402
from ml.weather.leakage import (  # noqa: E402
    assert_time_split_purity,
    parse_ts,
    validate_feature_names,
)

TASK_KIND = {
    "temperature": "regression",
    "rain_prob": "probability",
    "precip_amount": "regression",
    "wind": "regression",
    "pressure": "regression",
    "humidity": "regression",
}
TASK_UNITS = {
    "temperature": "degC",
    "rain_prob": "probability",
    "precip_amount": "mm",
    "wind": "m/s",
    "pressure": "hPa",
    "humidity": "%",
}

HYPERPARAMS = {
    "max_iter": 200,
    "learning_rate": 0.06,
    "max_leaf_nodes": 31,
    "min_samples_leaf": 20,
    "l2_regularization": 1.0,
    "random_state": 7,
    # max_bins 255 -> 127 (2026-11-06 accuracy update): halves the binned
    # feature + histogram memory during fits at ~6M pairs; HistGB accuracy
    # impact is negligible at this data volume.
    "max_bins": 127,
}


def git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT,
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "n/a"


def _make_estimator(task: str):
    kw = dict(HYPERPARAMS)
    if TASK_KIND[task] == "probability":
        return HistGradientBoostingClassifier(**kw)
    return HistGradientBoostingRegressor(**kw)


def _time_split(ds, val_frac: float = 0.2):
    # ds.valid_times is an int64 epoch array — split on epochs directly
    # (same tie-safe semantics as the string version, no per-row parsing).
    from ml.weather.leakage import time_split_by_epoch

    return time_split_by_epoch(ds.valid_times, val_frac)


def fit_location_aware(task, ds, tr_idx, min_loc_rows: int = 200):
    """Return ({location_key: estimator}, handling_description).

    Trains one model per location_id when it has >= min_loc_rows training
    rows, PLUS a global fallback model trained on every finite training
    row. The global fallback is what ``predict_with_estimators`` uses for
    locations with no model of their own — in particular locations that
    appear only in validation (e.g. newly added locations whose data
    starts after the time-split cutoff). Before 2026-10-17 the global
    model was trained only on sub-threshold locations, so when the sole
    training location reached the threshold there was NO global model at
    all and every validation row for the 49 new worldwide locations came
    back NaN — silently shrinking regression evaluation to Yangon only
    and crashing rain_prob calibration with "Input X contains NaN"."""
    y_all = ds.y[task]
    estimators: dict = {}
    by_loc: dict[int, list[int]] = {}
    for i in tr_idx:
        if np.isfinite(y_all[i]):
            # int(): location_ids is an int64 array; keep estimator keys
            # plain Python ints (as before) for joblib/manifest safety.
            by_loc.setdefault(int(ds.location_ids[i]), []).append(i)
    for loc, idxs in sorted(by_loc.items()):
        if len(idxs) >= min_loc_rows:
            est = _make_estimator(task)
            est.fit(ds.X[idxs], y_all[idxs])
            estimators[loc] = est
    all_idx = [i for loc in sorted(by_loc) for i in by_loc[loc]]
    if all_idx:
        est = _make_estimator(task)
        est.fit(ds.X[all_idx], y_all[all_idx])
        estimators["global"] = est
    per_loc = sorted(k for k in estimators if k != "global")
    handling = (
        f"per-location models for {per_loc} plus a global fallback "
        f"trained on all {len(all_idx)} training rows (covers locations "
        "with no model of their own)"
        if per_loc
        else "global fallback model (no location reached "
             f"{min_loc_rows} training rows)"
    )
    return estimators, handling


def predict_with_estimators(estimators: dict, ds, idx, task: str):
    """Predict with per-location/global estimators; falls back to global.

    Vectorized over validation rows: the old per-location Python-level
    scan built ~1000 boolean masks of len(idx) one Python element at a
    time (~720M interpreter iterations at current data volume), spiking
    RSS by gigabytes and taking tens of minutes. Masks are identical.
    """
    idx = np.asarray(idx)
    out = np.full(len(idx), np.nan)
    loc_arr = np.asarray(ds.location_ids, dtype=np.int64)[idx]
    per_loc = [k for k in estimators if k != "global"]
    prob = TASK_KIND[task] == "probability"

    def _predict(est, mask):
        sub = idx[mask]
        if prob:
            out[mask] = est.predict_proba(ds.X[sub])[:, 1]
        else:
            out[mask] = est.predict(ds.X[sub])

    for loc_key in per_loc:
        mask = loc_arr == loc_key
        if mask.any():
            _predict(estimators[loc_key], mask)
    if "global" in estimators:
        if per_loc:
            mask = ~np.isin(loc_arr, np.array(per_loc))
        else:
            mask = np.ones(len(idx), dtype=bool)
        if mask.any():
            _predict(estimators["global"], mask)
    return out


def _metrics_table(summary: dict, task: str) -> str:
    if TASK_KIND[task] == "probability":
        return (
            f"| metric | value |\n|---|---|\n"
            f"| Brier score | {summary['brier']:.4f} |\n"
            f"| calibration error (ECE) | "
            f"{summary['calibration_error']:.4f} |\n"
            f"| validation samples | {summary['n']} |"
        )
    return (
        f"| metric | value |\n|---|---|\n"
        f"| MAE ({TASK_UNITS[task]}) | {summary['mae']:.3f} |\n"
        f"| RMSE ({TASK_UNITS[task]}) | {summary['rmse']:.3f} |\n"
        f"| CRPS, empirical Gaussian ({TASK_UNITS[task]}) | "
        f"{summary['crps']:.3f} |\n"
        f"| validation samples | {summary['n']} |"
    )


def render_model_card(version: str, task: str, ds, tr_idx, va_idx,
                      estimators, handling: str, summary: dict,
                      hyperparams: dict, calibration_report: dict | None,
                      synthetic: bool) -> str:
    tpl = (ROOT / "ml" / "MODEL_CARD_TEMPLATE.md").read_text()
    tr_times = [ds.valid_times[i] for i in tr_idx]
    va_times = [ds.valid_times[i] for i in va_idx]
    locs = sorted(set(int(ds.location_ids[i]) for i in va_idx))
    calib_line = ""
    if calibration_report:
        rows = []
        for regime, rep in calibration_report["per_regime"].items():
            rows.append(
                f"| {regime} | {rep.get('method')} | "
                f"{rep.get('brier_before')} -> {rep.get('brier_after')} | "
                f"{rep.get('n')} |")
        calib_line = "\n\n### Per-regime calibration\n" + "\n".join(
            ["| regime | method | Brier before -> after | n |",
             "|---|---|---|---|"] + rows)
    card = tpl
    for key, val in {
        "{{version}}": version,
        "{{task}}": task,
        "{{task_kind}}": TASK_KIND[task],
        "{{dataset_version}}": ds.dataset_version,
        "{{feature_version}}": ds.params.get("feature_version", "v1"),
        "{{source_versions}}": "open-meteo (per source_registry)",
        "{{git_commit}}": git_commit(),
        "{{training_start}}": epoch_to_iso(min(tr_times)),
        "{{training_end}}": epoch_to_iso(max(tr_times)),
        "{{n_train}}": str(len(tr_idx)),
        "{{validation_start}}": epoch_to_iso(min(va_times)),
        "{{validation_end}}": epoch_to_iso(max(va_times)),
        "{{n_val}}": str(len(va_idx)),
        "{{locations}}": ", ".join(map(str, locs)),
        "{{algorithm}}": (
            "HistGradientBoostingClassifier"
            if TASK_KIND[task] == "probability"
            else "HistGradientBoostingRegressor"),
        "{{hyperparameters}}": json.dumps(hyperparams),
        "{{location_handling}}": handling,
        "{{metrics_table}}": _metrics_table(summary, task) + calib_line,
        "{{synthetic_note}}": (
            "SYNTHETIC SMOKE TEST — not a real model. Trained on a clearly "
            "labeled synthetic fixture for pipeline verification only; never "
            "deployed, never champion." if synthetic else
            "Trained on real collected forecast/observation data."),
    }.items():
        card = card.replace(key, val)
    return card


def train_task(db_path: str, task: str, *,
               models_dir: str | Path | None = None,
               val_frac: float = 0.2,
               min_loc_rows: int = 200,
               synthetic: bool = False,
               persist: bool = True,
               location_id: int | None = None,
               start: str | None = None,
               end: str | None = None) -> dict:
    """Train one task end-to-end. Returns a result dict with metrics."""
    if task not in TASKS:
        raise ValueError(f"unknown task {task!r}")
    models_dir = Path(models_dir or ROOT / "models")
    started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    run_id = f"run_{task}_{started.replace(':', '').replace('-', '')}"

    ds = build_dataset(db_path, location_id=location_id,
                       start=start, end=end)
    validate_feature_names(ds.feature_names)
    if ds.n_pairs < 30:
        raise ValueError(
            f"too few forecast/observation pairs ({ds.n_pairs}) to train "
            f"task {task!r}")

    version = registry.next_version(task, db_path)
    tr_idx, va_idx = _time_split(ds, val_frac)
    yv = ds.y[task][va_idx]
    finite = np.isfinite(yv)

    estimators, handling = fit_location_aware(task, ds, tr_idx, min_loc_rows)
    pred = predict_with_estimators(estimators, ds, va_idx, task)

    # Prediction coverage must be explicit: rows whose location has no
    # estimator come back NaN from predict_with_estimators, and evaluation
    # filters them silently. Report how many finite-target validation
    # rows actually received a prediction.
    pred_finite = np.isfinite(pred)
    n_val_target = int(finite.sum())
    n_val_predicted = int((finite & pred_finite).sum())

    calibration_report = None
    fitted_cal = None
    if TASK_KIND[task] == "probability":
        mask = finite & pred_finite
        cal = calib_mod.fit_calibrators(
            yv[mask], pred[mask],
            regime_names(ds.regimes[va_idx[mask]]))
        calibration_report = {k: v for k, v in cal.items()
                              if k != "_fitted"}
        fitted_cal = cal["_fitted"]
        pred = calib_mod.apply_calibrators(
            fitted_cal, pred,
            regime_names(ds.regimes[va_idx]))

    summary, score_rows = evaluate_task(
        task, yv, pred, ds.forecast_hours[va_idx],
        [int(ds.location_ids[i]) for i in va_idx],
        regime_names(ds.regimes[va_idx]),
        period_start=epoch_to_iso(min(ds.valid_times[i] for i in va_idx)),
        period_end=epoch_to_iso(max(ds.valid_times[i] for i in va_idx)),
        model_version=version,
    )

    result = {
        "run_id": run_id, "task": task, "version": version,
        "dataset_version": ds.dataset_version,
        "n_pairs": ds.n_pairs,
        "n_train": int(len(tr_idx)), "n_val": int(len(va_idx)),
        "n_val_finite_target": n_val_target,
        "n_val_predicted": n_val_predicted,
        "prediction_coverage": (
            round(n_val_predicted / n_val_target, 4) if n_val_target else None
        ),
        "metrics": summary,
        "calibration": calibration_report,
        "data_kind": "synthetic_smoke" if synthetic else "real",
        "deployed": False, "gate": None,
    }

    if persist:
        vdir = models_dir / version
        vdir.mkdir(parents=True, exist_ok=True)
        joblib.dump({
            "estimators": estimators,
            "feature_names": ds.feature_names,
            "task": task,
            "kind": TASK_KIND[task],
            "calibrators": fitted_cal,
            "dataset_version": ds.dataset_version,
            "data_kind": result["data_kind"],
        }, vdir / "artifact.joblib")
        manifest = {
            "version": version, "task": task,
            "kind": TASK_KIND[task],
            "dataset_version": ds.dataset_version,
            "feature_version": ds.params.get("feature_version", "v1"),
            "feature_names": ds.feature_names,
            "hyperparameters": HYPERPARAMS,
            "location_handling": handling,
            "estimators": [str(k) for k in estimators],
            "git_commit": git_commit(),
            "data_kind": result["data_kind"],
            "validation_metrics": summary,
        }
        (vdir / "manifest.json").write_text(json.dumps(manifest, indent=2))
        (vdir / "MODEL_CARD.md").write_text(render_model_card(
            version, task, ds, tr_idx, va_idx, estimators, handling,
            summary, HYPERPARAMS, calibration_report, synthetic))

        metrics_json = dict(summary)
        metrics_json["data_kind"] = result["data_kind"]
        registry.register_model_version(
            db_path, version=version, task=task,
            dataset_version=ds.dataset_version,
            feature_version=ds.params.get("feature_version", "v1"),
            training_start=epoch_to_iso(min(ds.valid_times[i] for i in tr_idx)),
            training_end=epoch_to_iso(max(ds.valid_times[i] for i in tr_idx)),
            validation_start=epoch_to_iso(min(ds.valid_times[i] for i in va_idx)),
            validation_end=epoch_to_iso(max(ds.valid_times[i] for i in va_idx)),
            metrics=metrics_json, hyperparameters=HYPERPARAMS,
            git_commit=git_commit())
        registry.record_model_scores(db_path, score_rows)
        notes = (
            "synthetic smoke test — not a real model; never deploy, "
            "never champion"
            if synthetic else
            f"trained on real data ({ds.n_pairs} pairs, "
            f"{ds.dataset_version})"
        )
        registry.record_training_run(
            db_path, run_id=run_id, task=task,
            dataset_version=ds.dataset_version, model_version=version,
            rows=ds.n_pairs, metrics=metrics_json, status="success",
            deployed=False, notes=notes)
        gate = registry.deploy_gate(
            db_path, task=task, candidate_version=version,
            candidate_metrics=metrics_json, run_id=run_id)
        result["gate"] = gate
        result["deployed"] = gate["deploy"]
        result["artifact_dir"] = str(vdir)
    return result


def main(argv: list[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--task", required=True, choices=list(TASKS))
    ap.add_argument("--models-dir", default=None)
    ap.add_argument("--synthetic", action="store_true")
    args = ap.parse_args(argv)
    res = train_task(args.db, args.task, models_dir=args.models_dir,
                     synthetic=args.synthetic)
    print(json.dumps({k: v for k, v in res.items()
                      if k not in ("calibration",)}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
