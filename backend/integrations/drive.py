"""Google Drive archive for AETHER Earth Intelligence.

Drive is an ARCHIVE mirror only — never primary storage.
Primary storage is always local SQLite (data/aether.db).

Transport: the user's CONNECTED Google account via the vendored
`hatch_gws_cli` (same account the user linked in chat). No service-account
keys are used or needed.

Activation rule: the archive is ACTIVE when
    AETHER_DRIVE_ROOT_FOLDER_ID  — Drive folder id (env), OR
    bootstrap/DRIVE_ROOT_ID.txt  — saved folder id (fallback)
is present AND `hatch_gws_cli drive status` reports connected.
If no root folder id is known, ensure_folders() creates the tree.

When unconfigured, DriveArchive is disabled and health() returns
"not_configured".

Folders per spec section 26:
    AETHER_EARTH_INTELLIGENCE/
        RAW/{WEATHER,EARTHQUAKE,TSUNAMI,SATELLITE,RADAR}
        PROCESSED/
        TRAINING_DATA/
        MODELS/
        VALIDATION/
        ALERT_ARCHIVE/
        REPORTS/
        BACKUPS/
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("aether.integrations.drive")

REPO_ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP_ROOT_ID = REPO_ROOT / "bootstrap" / "DRIVE_ROOT_ID.txt"
GWS_CLI = os.environ.get("AETHER_GWS_CLI", "/opt/hatch/bin/hatch_gws_cli")

ROOT_ID_ENV = "AETHER_DRIVE_ROOT_FOLDER_ID"

ROOT_FOLDER = "AETHER_EARTH_INTELLIGENCE"
RAW_CATEGORIES = ["WEATHER", "EARTHQUAKE", "TSUNAMI", "SATELLITE", "RADAR"]
ARCHIVE_FOLDERS: List[str] = [
    "RAW",
    "PROCESSED",
    "TRAINING_DATA",
    "MODELS",
    "VALIDATION",
    "ALERT_ARCHIVE",
    "REPORTS",
    "BACKUPS",
]

_connected_cache: Optional[bool] = None


def _cli(*args: str, timeout: int = 120) -> Dict[str, Any]:
    """Run hatch_gws_cli and return parsed JSON. Raises RuntimeError on failure."""
    try:
        proc = subprocess.run(
            [GWS_CLI, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("google workspace cli unavailable: %s" % exc) from exc
    if proc.returncode != 0:
        raise RuntimeError("gws cli failed: %s" % (proc.stderr or proc.stdout)[:300])
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("gws cli returned non-JSON output") from exc


def _cli_connected() -> bool:
    global _connected_cache
    if _connected_cache is not None:
        return _connected_cache
    try:
        status = _cli("drive", "status")
        _connected_cache = status.get("status") == "connected"
    except RuntimeError as exc:
        log.warning("Drive status check failed: %s", exc)
        _connected_cache = False
    return _connected_cache


def resolve_root_id() -> Optional[str]:
    rid = os.environ.get(ROOT_ID_ENV)
    if rid:
        return rid.strip()
    try:
        if BOOTSTRAP_ROOT_ID.is_file():
            return BOOTSTRAP_ROOT_ID.read_text(encoding="utf-8").strip() or None
    except OSError:
        pass
    return None


class DriveArchive:
    """Archive mirror into Google Drive. Disabled unless configured."""

    def __init__(self) -> None:
        self._root_id: Optional[str] = resolve_root_id()
        self._folders: Dict[str, str] = {}  # name -> drive id
        self._last_error: Optional[str] = None

    # ------------------------------------------------------------------ setup
    @property
    def enabled(self) -> bool:
        return _cli_connected()

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    @property
    def root_id(self) -> Optional[str]:
        return self._root_id

    # ------------------------------------------------------------------ folders
    def _find_folder(self, name: str, parent_id: str) -> Optional[str]:
        q = (
            "name = '%s' and mimeType = 'application/vnd.google-apps.folder' "
            "and '%s' in parents and trashed = false" % (name.replace("'", "\\'"), parent_id)
        )
        resp = _cli(
            "drive", "files", "list",
            "--params", json.dumps({"q": q, "pageSize": 10, "fields": "files(id,name)"}),
        )
        files = resp.get("files", [])
        return files[0]["id"] if files else None

    def _create_folder(self, name: str, parent_id: str) -> str:
        created = _cli(
            "drive", "files", "create",
            "--params", json.dumps({"ignoreDefaultVisibility": True}),
            "--json", json.dumps({
                "name": name,
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [parent_id],
            }),
        )
        return created["id"]

    def ensure_folders(self) -> Dict[str, str]:
        """Create the archive folder tree. Returns {path: 'created'|'exists'}."""
        if not self.enabled:
            tree = self._expected_tree()
            return {path: "skipped_not_configured" for path in tree}
        result: Dict[str, str] = {}
        try:
            if not self._root_id:
                self._root_id = self._create_folder(ROOT_FOLDER, "root")
                result[ROOT_FOLDER] = "created"
            else:
                result[ROOT_FOLDER] = "exists"
            for folder in ARCHIVE_FOLDERS:
                existing = self._find_folder(folder, self._root_id)
                if existing:
                    fid = existing
                    result["%s/%s" % (ROOT_FOLDER, folder)] = "exists"
                else:
                    fid = self._create_folder(folder, self._root_id)
                    result["%s/%s" % (ROOT_FOLDER, folder)] = "created"
                self._folders[folder] = fid
            raw_id = self._folders["RAW"]
            for cat in RAW_CATEGORIES:
                existing = self._find_folder(cat, raw_id)
                if existing:
                    result["%s/RAW/%s" % (ROOT_FOLDER, cat)] = "exists"
                else:
                    self._create_folder(cat, raw_id)
                    result["%s/RAW/%s" % (ROOT_FOLDER, cat)] = "created"
        except RuntimeError as exc:
            self._last_error = str(exc)
            result["_error"] = str(exc)[:200]
        return result

    @staticmethod
    def _expected_tree() -> List[str]:
        paths = [ROOT_FOLDER]
        for folder in ARCHIVE_FOLDERS:
            paths.append("%s/%s" % (ROOT_FOLDER, folder))
        for cat in RAW_CATEGORIES:
            paths.append("%s/RAW/%s" % (ROOT_FOLDER, cat))
        return paths

    # ------------------------------------------------------------------ upload
    def upload_parquet_or_json(self, local_path: str, category: str) -> Dict[str, Any]:
        """Archive a parquet or JSON file under RAW/<CATEGORY>. Returns stats.

        category must be one of RAW_CATEGORIES (case-insensitive).
        """
        if not self.enabled:
            return {"status": "skipped", "reason": "not_configured", "file": local_path}
        cat = category.upper()
        if cat not in RAW_CATEGORIES:
            raise ValueError(
                "Unknown category %r; expected one of %s" % (category, RAW_CATEGORIES)
            )
        if not os.path.isfile(local_path):
            return {"status": "error", "reason": "file not found", "file": local_path}
        try:
            if not self._folders:
                self.ensure_folders()
            raw_id = self._folders.get("RAW")
            parent_id = self._find_folder(cat, raw_id) if raw_id else None
            if not parent_id:
                return {"status": "error", "reason": "RAW/%s folder missing" % cat,
                        "file": local_path}
            started = time.time()
            created = _cli("drive", "+upload", os.path.abspath(local_path),
                           "--parent", parent_id)
        except RuntimeError as exc:
            self._last_error = str(exc)
            return {"status": "error", "reason": str(exc)[:200], "file": local_path}
        return {
            "status": "ok",
            "file": created.get("name", os.path.basename(local_path)),
            "drive_id": created.get("id"),
            "category": cat,
            "seconds": round(time.time() - started, 3),
        }

    def upload_backup(self, local_path: str, target_name: Optional[str] = None) -> Dict[str, Any]:
        """Upload a file into BACKUPS/. Returns stats."""
        if not self.enabled:
            return {"status": "skipped", "reason": "not_configured", "file": local_path}
        if not os.path.isfile(local_path):
            return {"status": "error", "reason": "file not found", "file": local_path}
        try:
            if not self._folders:
                self.ensure_folders()
            parent_id = self._folders.get("BACKUPS")
            if not parent_id:
                return {"status": "error", "reason": "BACKUPS folder missing",
                        "file": local_path}
            started = time.time()
            args = ["drive", "+upload", os.path.abspath(local_path), "--parent", parent_id]
            if target_name:
                args += ["--name", target_name]
            # The production DB grows ~120 MB/day since the worldwide
            # expansion (174 MB on 2026-10-17); the default 120 s CLI
            # timeout is too small for uploads of that size and a slow
            # network stalled one past it on 2026-10-17 15:27 +0630.
            created = _cli(*args, timeout=1800)
        except RuntimeError as exc:
            self._last_error = str(exc)
            return {"status": "error", "reason": str(exc)[:200], "file": local_path}
        return {
            "status": "ok",
            "file": created.get("name", os.path.basename(local_path)),
            "drive_id": created.get("id"),
            "seconds": round(time.time() - started, 3),
        }

    # Retention for BACKUPS/ (user decision 2026-10-17 21:44 +0630):
    # keep the newest 48 backups, delete the rest. Hourly full-DB backups
    # had accumulated to 283 files / 11.67 GB (90% of the 15 GB quota).
    # Size guard (added same evening, ahead of the user's 15 GB dataset
    # scale-up): as the DB grows, compressed backups grow with it, so the
    # kept set is ALSO capped at 8 GB total (never fewer than 7 newest) —
    # otherwise 48 backups of a 15 GB DB would exceed the Drive quota.
    BACKUP_RETENTION = 48
    BACKUP_MIN_KEEP = 7
    BACKUP_MAX_TOTAL_BYTES = 8 * 1024 ** 3

    def upload_backup_compressed(self, local_path: str,
                                 target_name: Optional[str] = None) -> Dict[str, Any]:
        """Gzip a DB backup and upload it into BACKUPS/ as <name>.gz.

        Same 1800 s upload timeout as upload_backup. Returns stats
        including original/compressed sizes so callers can verify the
        compression actually happened.
        """
        import gzip
        import shutil
        import tempfile

        if not self.enabled:
            return {"status": "skipped", "reason": "not_configured", "file": local_path}
        if not os.path.isfile(local_path):
            return {"status": "error", "reason": "file not found", "file": local_path}
        name = target_name or os.path.basename(local_path)
        if not name.endswith(".gz"):
            name += ".gz"
        tmp_path = None
        try:
            if not self._folders:
                self.ensure_folders()
            parent_id = self._folders.get("BACKUPS")
            if not parent_id:
                return {"status": "error", "reason": "BACKUPS folder missing",
                        "file": local_path}
            started = time.time()
            # Temp file on the same filesystem as the source DB: the
            # system temp dir is a 512 MB tmpfs on this VM while the
            # gzipped backup is ~450 MB and grows with the DB — two
            # sync_drive runs failed with ENOSPC (Errno 28) while the
            # data disk had ~50 GB free (2026-10-21).
            fd, tmp_path = tempfile.mkstemp(
                suffix=".db.gz",
                dir=os.path.dirname(os.path.abspath(local_path)))
            with os.fdopen(fd, "wb") as raw:
                with gzip.GzipFile(filename="", mode="wb", fileobj=raw,
                                   mtime=0) as gz:
                    with open(local_path, "rb") as src:
                        shutil.copyfileobj(src, gz, length=1024 * 1024)
            created = _cli("drive", "+upload", tmp_path, "--parent", parent_id,
                           "--name", name, timeout=1800)
            return {
                "status": "ok",
                "file": created.get("name", name),
                "drive_id": created.get("id"),
                "original_bytes": os.path.getsize(local_path),
                "compressed_bytes": os.path.getsize(tmp_path),
                "seconds": round(time.time() - started, 3),
            }
        except RuntimeError as exc:
            self._last_error = str(exc)
            return {"status": "error", "reason": str(exc)[:200], "file": local_path}
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def _list_backups(self) -> List[Dict[str, Any]]:
        """All files in BACKUPS/ with createdTime + size (paginated)."""
        if not self._folders:
            self.ensure_folders()
        parent_id = self._folders.get("BACKUPS")
        if not parent_id:
            return []
        files: List[Dict[str, Any]] = []
        token = None
        while True:
            params = {"q": "'%s' in parents and trashed = false" % parent_id,
                      "pageSize": 1000,
                      "fields": "nextPageToken,files(id,name,size,createdTime)"}
            if token:
                params["pageToken"] = token
            resp = _cli("drive", "files", "list", "--params",
                        json.dumps(params), timeout=300)
            files.extend(resp.get("files", []))
            token = resp.get("nextPageToken")
            if not token:
                return files

    def prune_backups(self, keep: int = BACKUP_RETENTION) -> Dict[str, Any]:
        """Delete all but the newest `keep` files in BACKUPS/, also
        capping the kept set at BACKUP_MAX_TOTAL_BYTES (never fewer than
        BACKUP_MIN_KEEP newest) so a growing DB cannot refill the quota.

        Permanent delete (Drive trash still counts against quota).
        Returns kept/deleted counts and freed bytes; per-file failures
        are counted, never raised.
        """
        if not self.enabled:
            return {"status": "skipped", "reason": "not_configured"}
        files = self._list_backups()
        files.sort(key=lambda f: f.get("createdTime", ""), reverse=True)
        excess = files[keep:]
        kept = files[:keep]
        total, cut = 0, len(kept)
        for i, f in enumerate(kept):
            total += int(f.get("size", 0))
            if total > self.BACKUP_MAX_TOTAL_BYTES and i + 1 >= self.BACKUP_MIN_KEEP:
                cut = i + 1
                break
        excess = kept[cut:] + excess
        kept = kept[:cut]
        deleted, errors, freed = 0, 0, 0
        for f in excess:
            try:
                _cli("drive", "files", "delete", "--params",
                     json.dumps({"fileId": f["id"]}), timeout=120)
                deleted += 1
                freed += int(f.get("size", 0))
            except RuntimeError:
                errors += 1
        return {"status": "ok", "kept": len(kept),
                "deleted": deleted, "errors": errors, "freed_bytes": freed}

    def list_folder_files(self, folder: str) -> List[Dict[str, Any]]:
        """List files directly inside a named archive folder (e.g. TRAINING_DATA).

        Returns [{"id", "name"}, ...]; [] when disabled or folder unresolvable.
        Single page (pageSize 1000) — archive folders here hold few files.
        """
        if not self.enabled:
            return []
        if folder not in ARCHIVE_FOLDERS:
            raise ValueError(
                "Unknown folder %r; expected one of %s" % (folder, ARCHIVE_FOLDERS)
            )
        if not self._folders:
            self.ensure_folders()
        parent_id = self._folders.get(folder)
        if not parent_id:
            return []
        q = "'%s' in parents and trashed = false" % parent_id
        resp = _cli(
            "drive", "files", "list",
            "--params", json.dumps(
                {"q": q, "pageSize": 1000, "fields": "files(id,name)"}),
        )
        return resp.get("files", [])

    def upload_to_folder(self, local_path: str, folder: str,
                         target_name: Optional[str] = None) -> Dict[str, Any]:
        """Upload a file into a named archive folder (ARCHIVE_FOLDERS). Returns stats.

        Same transport and 1800 s upload timeout as upload_backup: archive
        payloads (DB backups, training exports) can be tens of MB and the
        default 120 s CLI timeout is too small on a slow network.
        """
        if not self.enabled:
            return {"status": "skipped", "reason": "not_configured", "file": local_path}
        if folder not in ARCHIVE_FOLDERS:
            raise ValueError(
                "Unknown folder %r; expected one of %s" % (folder, ARCHIVE_FOLDERS)
            )
        if not os.path.isfile(local_path):
            return {"status": "error", "reason": "file not found", "file": local_path}
        try:
            if not self._folders:
                self.ensure_folders()
            parent_id = self._folders.get(folder)
            if not parent_id:
                return {"status": "error", "reason": "%s folder missing" % folder,
                        "file": local_path}
            started = time.time()
            args = ["drive", "+upload", os.path.abspath(local_path),
                    "--parent", parent_id]
            if target_name:
                args += ["--name", target_name]
            created = _cli(*args, timeout=1800)
        except RuntimeError as exc:
            self._last_error = str(exc)
            return {"status": "error", "reason": str(exc)[:200], "file": local_path}
        return {
            "status": "ok",
            "file": created.get("name", target_name or os.path.basename(local_path)),
            "drive_id": created.get("id"),
            "folder": folder,
            "seconds": round(time.time() - started, 3),
        }

    # ------------------------------------------------------------------ health
    def health(self) -> Dict[str, Any]:
        """Return integration health: not_configured | ok | down."""
        if not _cli_connected():
            return {
                "component": "drive",
                "status": "not_configured",
                "message": "Google Drive is not connected; link the account in chat.",
            }
        try:
            _cli("drive", "files", "list",
                 "--params", json.dumps({"pageSize": 1, "fields": "files(id)"}))
            return {"component": "drive", "status": "ok", "message": "drive api reachable"}
        except RuntimeError as exc:
            return {
                "component": "drive",
                "status": "down",
                "message": "drive api unreachable: %s" % str(exc)[:200],
            }
