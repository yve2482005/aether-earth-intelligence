"""Walk-forward backtest: "pretend it's date X, only use data <= X".

For each cutoff date in [start, end) stepping by ``step_days``:
  * build training pairs with valid_time <= cutoff, time-split into
    train/validation internally (validation used for probability calibration)
  * fit the task model on the train split only
  * build a SEPARATE evaluation set with cutoff < valid_time <= cutoff+step
  * score predictions against those later observations

No future observations, alerts, or revisions ever enter training features;
the evaluation window is strictly after the training cutoff. Each fold is
a research diagnostic — no registry writes, no artifacts.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ml.weather import calibrate as calib_mod  # noqa: E402
from ml.weather.dataset import (  # noqa: E402
    TASKS, build_dataset, regime_names)
from ml.weather.evaluate import evaluate_task  # noqa: E402
from ml.weather.leakage import assert_time_split_purity, parse_ts  # noqa: E402
from ml.weather.train import (  # noqa: E402
    TASK_KIND,
    fit_location_aware,
    predict_with_estimators,
)

MIN_TRAIN_PAIRS = 30
MIN_EVAL_PAIRS = 10


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def make_cutoffs(start: str, end: str, step_days: int) -> list[str]:
    s, e = parse_ts(start), parse_ts(end)
    out, cur = [], s
    while cur < e:
        out.append(_iso(cur))
        cur += timedelta(days=step_days)
    return out


def _time_split_indices(valid_times, val_frac):
    # valid_times is an int64 epoch array — split on epochs directly.
    from ml.weather.leakage import time_split_by_epoch

    return time_split_by_epoch(valid_times, val_frac)


def walk_forward(db_path: str, task: str, start: str, end: str,
                 step_days: int = 7, val_frac: float = 0.25,
                 min_loc_rows: int = 200) -> list[dict]:
    """Run the walk-forward loop. Returns one metrics dict per fold."""
    if task not in TASKS:
        raise ValueError(f"unknown task {task!r}")
    folds: list[dict] = []
    for k, cutoff in enumerate(make_cutoffs(start, end, step_days)):
        fold_end = _iso(parse_ts(cutoff) + timedelta(days=step_days))
        fold = {
            "fold": k,
            "train_end": cutoff,   # training pairs: valid_time <= cutoff
            "eval_start": cutoff,  # eval pairs: cutoff < valid_time <= fold_end
            "eval_end": fold_end,
            "status": "ok",
        }
        try:
            ds_tr = build_dataset(db_path, end=cutoff)
            if ds_tr.n_pairs < MIN_TRAIN_PAIRS:
                fold["status"] = (
                    f"skipped: only {ds_tr.n_pairs} training pairs "
                    f"(need {MIN_TRAIN_PAIRS})")
                folds.append(fold)
                continue
            tr_idx, va_idx = _time_split_indices(ds_tr.valid_times, val_frac)
            yv = ds_tr.y[task][va_idx]
            if int(np.isfinite(yv).sum()) < 5:
                fold["status"] = "skipped: <5 finite validation targets"
                folds.append(fold)
                continue
            estimators, _ = fit_location_aware(task, ds_tr, tr_idx,
                                               min_loc_rows)
            pred_va = predict_with_estimators(estimators, ds_tr, va_idx, task)
            fitted_cal = None
            if TASK_KIND[task] == "probability":
                mask = np.isfinite(yv)
                cal = calib_mod.fit_calibrators(
                    yv[mask], pred_va[mask],
                    regime_names(ds_tr.regimes[va_idx[mask]]))
                fitted_cal = cal["_fitted"]

            ds_ev = build_dataset(db_path, start=cutoff, end=fold_end)
            ye = ds_ev.y[task]
            finite = np.isfinite(ye)
            if int(finite.sum()) < MIN_EVAL_PAIRS:
                fold["status"] = (
                    f"skipped: only {int(finite.sum())} eval pairs "
                    f"(need {MIN_EVAL_PAIRS})")
                folds.append(fold)
                continue
            ev_idx = np.nonzero(finite)[0]
            pred_ev = predict_with_estimators(
                estimators, ds_ev, ev_idx, task)
            if fitted_cal is not None:
                pred_ev = calib_mod.apply_calibrators(
                    fitted_cal, pred_ev,
                    regime_names(ds_ev.regimes[ev_idx]))
            summary, _ = evaluate_task(
                task, ye[ev_idx], pred_ev, ds_ev.forecast_hours[ev_idx],
                [int(ds_ev.location_ids[i]) for i in ev_idx],
                regime_names(ds_ev.regimes[ev_idx]),
                period_start=cutoff, period_end=fold_end,
                model_version=f"backtest_fold{k}",
            )
            fold["n_train_pairs"] = ds_tr.n_pairs
            fold["n_eval_pairs"] = int(finite.sum())
            for m, v in summary.items():
                if m != "reliability_bins" and v is not None:
                    fold[f"eval_{m}"] = round(float(v), 5)
        except Exception as exc:  # never silently pass: record the failure
            fold["status"] = f"error: {type(exc).__name__}: {exc}"
        folds.append(fold)
    return folds


def render_markdown(task: str, folds: list[dict]) -> str:
    kind = TASK_KIND[task]
    metric_cols = (["eval_brier", "eval_calibration_error"]
                   if kind == "probability"
                   else ["eval_mae", "eval_rmse", "eval_crps"])
    head = ("| fold | train_end (data <=) | eval window | n_train | n_eval | "
            + " | ".join(metric_cols) + " | status |")
    sep = "|" + "---|" * (6 + len(metric_cols))
    lines = [
        f"# Walk-forward backtest — {task}",
        "",
        "Each fold trains only on pairs with valid_time <= train_end and is "
        "scored on strictly later observations. No future data enters "
        "training.",
        "",
        head, sep,
    ]
    for f in folds:
        cells = [str(f["fold"]), f["train_end"],
                 f"{f['eval_start'][:10]}..{f['eval_end'][:10]}",
                 str(f.get("n_train_pairs", "-")),
                 str(f.get("n_eval_pairs", "-"))]
        for c in metric_cols:
            v = f.get(c)
            cells.append(f"{v:.4f}" if isinstance(v, float) else "-")
        cells.append(f["status"])
        lines.append("| " + " | ".join(cells) + " |")
    lines += ["",
              "Metrics describe past agreement between predictions and "
              "observations (data confidence), not certainty about the future."]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--task", required=True, choices=list(TASKS))
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--step-days", type=int, default=7)
    args = ap.parse_args(argv)
    folds = walk_forward(args.db, args.task, args.start, args.end,
                         step_days=args.step_days)
    print(render_markdown(args.task, folds))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
