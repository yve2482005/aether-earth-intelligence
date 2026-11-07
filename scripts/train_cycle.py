#!/usr/bin/env python3
"""1 GB train-and-clean cycle (user order 2026-10-17 22:07 +0630).

Measures RAW weather data (weather_forecasts + weather_observations via
dbstat) against data/train_cycle_state.json. When growth since the last
completed cycle reaches 1 GiB:

  1. retrain all 6 tasks (champion gate applies inside the trainer)
  2. regenerate predictions (scripts/run_predict.py)
  3. export the training dataset to Drive (scripts/export_training_data.py)
  4. ONLY THEN delete the consumed raw rows — forecasts/observations with
     timestamps <= the cycle's dataset period end — and update the state
     file IMMEDIATELY after the delete commits (the delete is the
     irreversible step; the state must reflect it even if space reclaim
     fails afterwards). VACUUM/ANALYZE runs best-effort after that.
  5. print a JSON summary

A cycle whose shared training dataset has fewer than MIN_CYCLE_PAIRS
pairs aborts BEFORE deletion: that means the measured growth came from
an already-consumed window (state desync), not from fresh data.

Any step failing aborts the cycle BEFORE deletion and exits non-zero.
Earthquakes, alerts, tsunami, ocean and every other table are never
deleted. First run (no state file) only records the baseline.

Usage: train_cycle.py [--force]   (--force skips the 1 GiB threshold)
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from backend.common.config import DB_PATH  # noqa: E402

VENV_PY = str(REPO_ROOT.parent / ".tooling" / "aether-venv" / "bin" / "python")
STATE_PATH = REPO_ROOT / "data" / "train_cycle_state.json"
THRESHOLD_BYTES = 1024 ** 3  # 1 GiB
# A genuine >= 1 GiB cycle yields >= ~250k forecast/observation pairs
# (cycle 1: 1,053,535). A far smaller dataset means the window was
# already consumed by an earlier partial cycle; abort before deletion.
MIN_CYCLE_PAIRS = 100_000
TASKS = ("temperature", "rain_prob", "precip_amount", "wind", "pressure",
         "humidity")


def raw_bytes(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(pgsize), 0) FROM dbstat "
            "WHERE name IN ('weather_forecasts', 'weather_observations')"
        ).fetchone()
        return int(row[0])
    finally:
        conn.close()


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"raw_bytes_after_last_cycle": None, "last_period_end": None,
            "cycles": 0}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2))


def run_step(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=REPO_ROOT, capture_output=True, text=True)


def main() -> int:
    force = "--force" in sys.argv
    state = load_state()
    current = raw_bytes(str(DB_PATH))
    if state["raw_bytes_after_last_cycle"] is None:
        state["raw_bytes_after_last_cycle"] = current
        save_state(state)
        print(json.dumps({"status": "baseline_set", "raw_bytes": current}))
        return 0
    growth = current - state["raw_bytes_after_last_cycle"]
    if growth < THRESHOLD_BYTES and not force:
        print(json.dumps({"status": "below_threshold", "growth_bytes": growth,
                          "threshold_bytes": THRESHOLD_BYTES}))
        return 0

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    summary: dict = {"status": "training", "growth_bytes": growth, "tasks": {}}
    period_ends: list[str] = []

    # 1. Retrain all tasks — every one must succeed.
    for task in TASKS:
        log = REPO_ROOT / "training_logs" / f"cycle_{stamp}_{task}.json"
        err = REPO_ROOT / "training_logs" / f"cycle_{stamp}_{task}.err"
        proc = run_step([VENV_PY, "-m", "ml.weather.train", "--db",
                         str(DB_PATH), "--task", task])
        log.write_text(proc.stdout)
        err.write_text(proc.stderr)
        if proc.returncode != 0:
            summary.update(status="failed_at_train", task=task,
                           stderr=proc.stderr[-500:])
            print(json.dumps(summary))
            return 1
        result = json.loads(proc.stdout)
        gate = result.get("gate") or {}
        summary["tasks"][task] = {
            "version": result.get("version"),
            "n_pairs": result.get("n_pairs"),
            "coverage": result.get("prediction_coverage"),
            "deployed": gate.get("deploy"),
            "gate_reason": gate.get("reason", "")[:120],
        }
        for key in ("training_end", "validation_end"):
            metrics = result.get("metrics") or {}
            end = (result.get(key) or metrics.get(key))
            if end:
                period_ends.append(str(end))

    # Dataset-size guard: all tasks train on the same shared dataset. A
    # small dataset means the window was already consumed by an earlier
    # partial cycle whose state failed to advance; abort BEFORE predict /
    # export / delete so the remaining data survives intact.
    min_pairs = min((t.get("n_pairs") or 0)
                    for t in summary["tasks"].values())
    summary["min_n_pairs"] = min_pairs
    if min_pairs < MIN_CYCLE_PAIRS:
        summary.update(status="failed_small_dataset",
                       min_required=MIN_CYCLE_PAIRS)
        print(json.dumps(summary))
        return 1

    # The dataset period end: max valid_time actually present in pairs.
    conn = sqlite3.connect(str(DB_PATH))
    try:
        period_end = conn.execute(
            "SELECT MAX(valid_time) FROM weather_forecasts "
            "WHERE valid_time <= (SELECT MAX(observed_at) "
            "                     FROM weather_observations)"
        ).fetchone()[0]
    finally:
        conn.close()
    if not period_end:
        summary.update(status="failed_no_period_end")
        print(json.dumps(summary))
        return 1
    summary["period_end"] = period_end
    if state.get("last_period_end") and period_end <= state["last_period_end"]:
        summary.update(status="failed_no_new_period",
                       last_period_end=state["last_period_end"])
        print(json.dumps(summary))
        return 1

    # 2. Predictions with the resulting champions.
    proc = run_step([VENV_PY, "scripts/run_predict.py"])
    if proc.returncode != 0:
        summary.update(status="failed_at_predict", stderr=proc.stderr[-500:])
        print(json.dumps(summary))
        return 1

    # 3. Training-data export to Drive (new version uploads; skip is ok).
    proc = run_step([VENV_PY, "scripts/export_training_data.py"])
    if proc.returncode != 0:
        summary.update(status="failed_at_export", stderr=proc.stderr[-500:])
        print(json.dumps(summary))
        return 1
    summary["export_tail"] = proc.stdout.strip().splitlines()[-1][:300]

    # 4. Delete ONLY the consumed raw rows. The delete is the cycle's
    # irreversible step, so the state file is updated IMMEDIATELY after
    # it commits — before VACUUM — so a space-reclaim failure can never
    # leave the state pointing at already-deleted data. (2026-10-21
    # incident: VACUUM raised "database or disk is full" after the
    # delete committed; the state stayed at the previous cycle, and
    # later passes retrained on — and re-deleted — the consumed window,
    # promoting degenerate narrow-window models.)
    conn = sqlite3.connect(str(DB_PATH))
    try:
        before = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("weather_forecasts", "weather_observations")}
        d1 = conn.execute("DELETE FROM weather_forecasts WHERE valid_time <= ?",
                          (period_end,)).rowcount
        d2 = conn.execute(
            "DELETE FROM weather_observations WHERE observed_at <= ?",
            (period_end,)).rowcount
        conn.commit()
    finally:
        conn.close()
    summary["deleted"] = {"weather_forecasts": d1,
                          "weather_observations": d2, "before": before}

    # 5. State first — the durable record of the deletion.
    state["raw_bytes_after_last_cycle"] = raw_bytes(str(DB_PATH))
    state["last_period_end"] = period_end
    state["cycles"] = int(state.get("cycles", 0)) + 1
    save_state(state)

    # 6. Reclaim space — best effort. A failure here does not fail the
    # cycle and never touches the state.
    vacuum_ok = True
    try:
        conn = sqlite3.connect(str(DB_PATH))
        try:
            conn.execute("VACUUM")
            conn.execute("ANALYZE")
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        vacuum_ok = False
        summary["vacuum_error"] = str(exc)[:200]
    summary["vacuum_ok"] = vacuum_ok
    summary.update(status="cycle_complete", cycle=state["cycles"],
                   raw_bytes_after=state["raw_bytes_after_last_cycle"])
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
