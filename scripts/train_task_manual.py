"""Manual single-task train run (no persistence, no gate, no champion impact).

Used to prove a trainer/dataset change at full worldwide scale before the
hourly pass attempts cycle #2. persist=False skips registry writes, the
deploy gate and artifact persistence, so this is pure compute.

Usage:
    python scripts/train_task_manual.py temperature
    python scripts/train_task_manual.py temperature --location-id 1
"""
from __future__ import annotations

import argparse
import json
import resource
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ml.weather.train import train_task  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("task")
    ap.add_argument("--location-id", type=int, default=None)
    ap.add_argument("--db", default=str(ROOT / "data" / "aether.db"))
    args = ap.parse_args()
    res = train_task(args.db, args.task, location_id=args.location_id,
                     persist=False)
    peak_gb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1_048_576
    print("MANUAL TRAIN OK task=", res["task"],
          "n_pairs=", res["n_pairs"],
          "dataset=", res["dataset_version"])
    print("metrics=", json.dumps(res["metrics"], default=str))
    print(f"peak_rss_gb={peak_gb:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
