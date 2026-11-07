#!/usr/bin/env python3
"""Archive a timestamped SQLite backup into Google Drive BACKUPS/.

Uses backend.integrations.drive.DriveArchive with the user's connected
Google account. Safe to run after every collection.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from backend.common.config import DB_PATH  # noqa: E402
from backend.common.logging_setup import setup_logging  # noqa: E402
from backend.integrations.drive import DriveArchive  # noqa: E402

log = setup_logging("aether.sync_drive")


def main() -> None:
    archive = DriveArchive()
    print("drive enabled:", archive.enabled)
    if not archive.enabled:
        print("Drive not connected; nothing to do.")
        print(json.dumps(archive.health(), indent=1))
        return
    folders = archive.ensure_folders()
    created = sum(1 for v in folders.values() if v == "created")
    print(f"ensure_folders: {created} created, rest exist")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = f"aether-{stamp}.db"
    # Compressed backups + keep-48 retention (user decision 2026-10-17):
    # uncompressed hourly backups had filled 90% of the Drive quota.
    out = archive.upload_backup_compressed(str(DB_PATH), target_name=target)
    print("upload_backup_compressed:", json.dumps(out, indent=1)[:500])
    if out.get("status") == "ok":
        print("prune_backups:", json.dumps(archive.prune_backups(), indent=1)[:300])
    print("\nhealth:", json.dumps(archive.health()))


if __name__ == "__main__":
    main()
