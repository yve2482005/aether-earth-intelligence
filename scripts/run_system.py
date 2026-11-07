#!/usr/bin/env python3
"""AETHER — ONE-SYSTEM full pass (user order 2026-10-17 22:10 +0630:
"တခါထဲ လုပ်မဲ့ စနစ်" — one system that does everything in one run).

A single invocation runs every stage of the global weather system in
order and prints ONE consolidated JSON report:

  1. collect        advanced collector (all due location tiers)
  2. bulk_sweep     200k-city global places sweep segment (checkpointed;
                    bounded by --bulk-batches; skipped if not installed)
  3. train_cycle    1 GB train -> predict -> export -> delete-consumed
                    cycle (scripts/train_cycle.py; skips itself below 1 GiB)
  4. predict        hourly prediction refresh (scripts/run_predict.py)
  5. sync_sheets    full Sheets mirror + daily archives + training book
  6. sync_drive     compressed DB backup + keep-48/size-guard prune

Stages are isolated: one stage failing never stops the rest; the report
carries each stage's exit code and output tail, and the process exits
non-zero if any stage failed. The 5-minute aether-collect job remains
the system's sensor stage (earthquake/tsunami alerts cannot wait for a
full pass); everything else runs through this entry point.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
VENV_PY = str(REPO_ROOT.parent / ".tooling" / "aether-venv" / "bin" / "python")


def run_stage(name: str, argv: list[str], timeout: int) -> dict:
    started = time.time()
    try:
        proc = subprocess.run(argv, cwd=REPO_ROOT, capture_output=True,
                              text=True, timeout=timeout)
        tail = (proc.stdout.strip().splitlines() or [""])[-1][:300]
        err_tail = (proc.stderr.strip().splitlines() or [""])[-1][:300]
        return {"stage": name, "exit": proc.returncode,
                "seconds": round(time.time() - started, 1),
                "tail": tail, "err_tail": err_tail if proc.returncode else ""}
    except subprocess.TimeoutExpired:
        return {"stage": name, "exit": -1,
                "seconds": round(time.time() - started, 1),
                "tail": f"TIMEOUT after {timeout}s", "err_tail": ""}
    except FileNotFoundError:
        return {"stage": name, "exit": -2,
                "seconds": 0.0, "tail": "stage script missing", "err_tail": ""}


# --- writer serialization (added 2026-10-18) --------------------------------
# The 5-minute collector and this hourly pass are the system's two writer
# families on one SQLite file. The collector flock-guards itself, but the
# bulk/train/predict stages used to write concurrently with it and both
# sides lost to "database is locked" (first pass: bulk_sweep + predict
# failed; concurrent collectors were locked out for whole 20-min runs).
# The pass now holds the SAME flock as the collector across its writer
# stages: while held, 5-min collectors skip cleanly (their global stage
# is covered by this pass's own collect stage), and the writer stages run
# without contention. Bounded wait: a collector run is capped at 20 min,
# so 25 min of patience always suffices in the worst case.
import fcntl  # noqa: E402

_LOCK_PATH = REPO_ROOT / "data" / ".collect_advanced.lock"
_lock_file = None
# bulk_sweep is deliberately NOT a writer-lock stage (changed 2026-10-18):
# it writes one small batch at a time through its own bounded lock-retry
# and checkpoints every batch, so it tolerates collector concurrency —
# while holding the flock across a 200-batch sweep let one proxy-wedged
# fetch stall ALL 5-minute collection for 53+ minutes. The lock exists
# for the stages that truly need exclusivity: train_cycle (bulk delete +
# VACUUM) and predict (large batch writes).
_WRITER_STAGES = {"train_cycle", "predict"}


def _acquire_writer_lock(timeout_s: int = 1500) -> bool:
    global _lock_file
    _lock_file = open(_LOCK_PATH, "w")
    deadline = time.time() + timeout_s
    while True:
        try:
            fcntl.flock(_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if time.time() >= deadline:
                return False
            time.sleep(20)


def _release_writer_lock() -> None:
    global _lock_file
    if _lock_file is not None:
        try:
            fcntl.flock(_lock_file, fcntl.LOCK_UN)
            _lock_file.close()
        except Exception:  # noqa: BLE001
            pass
        _lock_file = None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bulk-batches", type=int, default=200,
                    help="max global-places batches this pass (0 = skip)")
    ap.add_argument("--skip", default="",
                    help="comma-separated stage names to skip")
    args = ap.parse_args()
    skip = {s.strip() for s in args.skip.split(",") if s.strip()}

    stages: list[tuple[str, list[str], int]] = [
        # The collect stage carries the same 20-minute hard cap as the
        # 5-minute cron (timeout wrapper added 2026-10-18): a proxy-wedged
        # collector inside a pass otherwise holds the flock for the full
        # 1800s stage timeout and starves every 5-minute run meanwhile.
        ("collect", ["timeout", "-k", "30", "1200",
                     VENV_PY, "scripts/run_collect_advanced.py"], 1500),
        # GDACS disaster feed (added 2026-11-06): global EQ/TC/FL/VO/DR/WF
        # events, free, no key, verified live. Non-writer stage; a failure
        # never stops the rest of the pass (stages are isolated).
        ("disaster_gdacs", [VENV_PY, "scripts/collect_gdacs.py",
                            "--days", "7"], 600),
        ("bulk_sweep", ["timeout", "-k", "30", "1400",
                        VENV_PY, "scripts/collect_global_places.py",
                        "--max-batches", str(args.bulk_batches)], 1500),
        # 12600s (3.5h; raised from 7200 on 2026-10-23): at 4.44M pairs
        # (backlog cycle #2) the 6 tasks need ~2h50m — dataset build ~10m
        # + fit/predict/evaluate ~18m each — so the 2h cap killed the
        # stage with 5/6 tasks done and discarded the whole cycle.
        # Post-backlog cycles train on ~1M pairs (~1.25h), well inside.
        ("train_cycle", [VENV_PY, "scripts/train_cycle.py"], 12600),
        ("predict", [VENV_PY, "scripts/run_predict.py"], 1200),
        # 3300s (raised from 2400 on 2026-10-18): with the per-call
        # timeout fix the sync no longer hangs, but a full mirror takes
        # ~2365s in production — the 2400 cap made completion a coin
        # flip (05:11 pass timed out at exactly 2400s).
        # nice/ionice (added 2026-10-18): the Sheets sync's snapshot
        # scans + subprocess churn saturate this VM's single disk; while
        # it ran at full priority the 5-minute collectors repeatedly got
        # ZERO rows (write transactions stretched past busy_timeout by
        # IO latency). The syncs are batch work — run them at idle IO
        # priority so collectors always win the disk. (If nice/ionice
        # are absent the stage fails loudly as 'stage script missing'
        # rather than silently unprioritized.)
        ("sync_sheets", ["nice", "-n", "10", "ionice", "-c", "3",
                         VENV_PY, "scripts/sync_sheets.py"], 3300),
        ("sync_drive", ["nice", "-n", "10", "ionice", "-c", "3",
                        VENV_PY, "scripts/sync_drive.py"], 3600),
    ]
    report = {"system": "aether", "stages": []}
    failures = 0
    lock_held = False
    lock_failed = False
    for name, argv, timeout in stages:
        if name in _WRITER_STAGES and not lock_held and not lock_failed:
            lock_held = _acquire_writer_lock()
            lock_failed = not lock_held
        if name in skip or (name == "bulk_sweep" and args.bulk_batches == 0):
            report["stages"].append({"stage": name, "exit": None,
                                     "tail": "skipped"})
            continue
        if name in _WRITER_STAGES and lock_failed:
            report["stages"].append(
                {"stage": name, "exit": None,
                 "tail": "skipped_lock: writer lock held by a collector "
                         "run for the whole wait window; deferred to "
                         "the next pass"})
            continue
        if name == "bulk_sweep" and not (REPO_ROOT / "scripts" /
                                         "collect_global_places.py").exists():
            report["stages"].append({"stage": name, "exit": None,
                                     "tail": "not installed yet"})
            continue
        result = run_stage(name, argv, timeout)
        if result["exit"] not in (0, None):
            failures += 1
        report["stages"].append(result)
        if name == "predict" and lock_held:
            _release_writer_lock()
            lock_held = False
    if lock_held:
        _release_writer_lock()
    report["failures"] = failures
    print(json.dumps(report, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
