#!/usr/bin/env python3
"""Export the CURRENT ML training dataset to Google Drive TRAINING_DATA/.

Separate pipeline from the Sheets sync and the DB-backup sync: training
data is versioned by the trainer's own content hash (dataset_version,
ds_...), so each distinct dataset is archived exactly once.

The pairs are built with ml.weather.dataset.build_dataset — the exact
function training consumes, never a re-implementation — and written as a
gzipped CSV: one row per forecast/observation pair with valid_time,
location_id, regime, every feature column, and every task target
(target_<task>). NaN values are written as empty fields.

Idempotent by dataset_version: if TRAINING_DATA/ already holds a file
for this dataset_version, the upload is skipped. A local registry
(data/training_registry.csv) records every export run.

Nothing is ever deleted — not locally, not in Drive.
"""
import csv
import gzip
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from backend.common.config import DATA_DIR, DB_PATH  # noqa: E402
from backend.common.logging_setup import setup_logging  # noqa: E402
from backend.integrations.drive import DriveArchive  # noqa: E402
from ml.weather.dataset import REGIMES, TASKS, build_dataset, epoch_to_iso  # noqa: E402

log = setup_logging("aether.export_training_data")

DRIVE_FOLDER = "TRAINING_DATA"
REGISTRY_FIELDS = [
    "dataset_version", "exported_at", "pair_rows", "period_start",
    "period_end", "locations_count", "drive_file_name", "drive_file_id",
]


def _fmt(v: float) -> str:
    """CSV cell for a float: NaN -> empty (portable missing-value marker)."""
    f = float(v)
    return "" if math.isnan(f) else repr(f)


def write_pairs_csv(ds, out_path: Path) -> int:
    """Write the dataset's pairs as gzipped CSV. Returns the row count."""
    target_cols = [f"target_{t}" for t in TASKS]
    header = ["valid_time", "location_id", "regime"] + list(ds.feature_names) + target_cols
    n = 0
    with gzip.open(out_path, "wt", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        for i in range(ds.n_pairs):
            row: List[str] = [
                epoch_to_iso(int(ds.valid_times[i])),
                str(int(ds.location_ids[i])),
                REGIMES[int(ds.regimes[i])],
            ]
            row.extend(_fmt(v) for v in ds.X[i])
            row.extend(_fmt(ds.y[t][i]) for t in TASKS)
            w.writerow(row)
            n += 1
    return n


def append_registry(registry_path: Path, row: Dict[str, Any]) -> None:
    """Append one export record to the local registry CSV (created if new)."""
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    exists = registry_path.is_file()
    with registry_path.open("a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=REGISTRY_FIELDS)
        if not exists:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in REGISTRY_FIELDS})


def export_training_data(db_path: str,
                         export_dir: Path,
                         registry_path: Path,
                         archive: Optional[DriveArchive] = None,
                         today: Optional[datetime] = None) -> Dict[str, Any]:
    """Build the current dataset, write the pairs CSV, archive it to Drive.

    Returns a JSON-able summary. Drive outcomes:
      uploaded        — new file created in TRAINING_DATA/
      skipped         — this dataset_version is already stored in Drive
      not_configured  — Drive not connected; local export still written
      error           — Drive upload failed; local export still written
    """
    today = today or datetime.now(timezone.utc)
    ds = build_dataset(db_path)
    locations = sorted({int(x) for x in ds.location_ids})
    filename = f"training_pairs_{ds.dataset_version}_{today:%Y%m%d}.csv.gz"

    export_dir.mkdir(parents=True, exist_ok=True)
    local_path = export_dir / filename
    rows = write_pairs_csv(ds, local_path)
    file_bytes = local_path.stat().st_size

    archive = archive if archive is not None else DriveArchive()
    drive_file_name = ""
    drive_file_id = ""
    if not archive.enabled:
        status = "not_configured"
        drive: Dict[str, Any] = {"status": "skipped", "reason": "not_configured"}
    else:
        existing = archive.list_folder_files(DRIVE_FOLDER)
        match = next((f for f in existing
                      if ds.dataset_version in f.get("name", "")), None)
        if match is not None:
            status = "skipped"
            drive = {"status": "skipped", "reason": "version already stored",
                     "file": match.get("name"), "drive_id": match.get("id")}
            drive_file_name = match.get("name", "")
            drive_file_id = match.get("id", "")
        else:
            drive = archive.upload_to_folder(str(local_path), DRIVE_FOLDER,
                                             target_name=filename)
            if drive.get("status") == "ok":
                status = "uploaded"
                drive_file_name = drive.get("file", filename)
                drive_file_id = drive.get("drive_id", "")
            else:
                status = "error"

    exported_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    append_registry(registry_path, {
        "dataset_version": ds.dataset_version,
        "exported_at": exported_at,
        "pair_rows": rows,
        "period_start": ds.time_min,
        "period_end": ds.time_max,
        "locations_count": len(locations),
        "drive_file_name": drive_file_name,
        "drive_file_id": drive_file_id,
    })

    return {
        "status": status,
        "dataset_version": ds.dataset_version,
        "pairs": rows,
        "period_start": ds.time_min,
        "period_end": ds.time_max,
        "locations_count": len(locations),
        "file": filename,
        "local_path": str(local_path),
        "file_bytes": file_bytes,
        "drive": drive,
        "registry": str(registry_path),
    }


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(DB_PATH), help="SQLite DB path")
    ap.add_argument("--export-dir", default=str(DATA_DIR / "training_exports"),
                    help="local folder for the gzipped pairs CSV")
    ap.add_argument("--registry", default=str(DATA_DIR / "training_registry.csv"),
                    help="local registry CSV path")
    args = ap.parse_args(argv)

    summary = export_training_data(
        db_path=args.db,
        export_dir=Path(args.export_dir),
        registry_path=Path(args.registry),
    )
    print(json.dumps(summary, indent=1))
    return 1 if summary["status"] == "error" else 0


if __name__ == "__main__":
    sys.exit(main())
