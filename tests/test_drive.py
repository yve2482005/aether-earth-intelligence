"""Unit tests for backend/integrations/drive.py (connected-account transport, mocked CLI)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from backend.integrations import drive  # noqa: E402
from backend.integrations.drive import (  # noqa: E402
    ARCHIVE_FOLDERS,
    RAW_CATEGORIES,
    ROOT_FOLDER,
    DriveArchive,
    resolve_root_id,
)


@pytest.fixture(autouse=True)
def _reset(monkeypatch, tmp_path):
    monkeypatch.delenv("AETHER_DRIVE_ROOT_FOLDER_ID", raising=False)
    monkeypatch.setattr(drive, "_connected_cache", None)
    monkeypatch.setattr(drive, "BOOTSTRAP_ROOT_ID", tmp_path / "DRIVE_ROOT_ID.txt")
    yield
    monkeypatch.setattr(drive, "_connected_cache", None)


def _mock_cli(monkeypatch, connected=True, folders=()):
    """folders: iterable of (name, parent_id, drive_id) the fake Drive contains."""
    calls = []

    def fake_cli(*args):
        calls.append(args)
        if args[:2] == ("drive", "status"):
            return {"status": "connected" if connected else "not_connected"}
        if args[1:3] == ("files", "list"):
            import json as _json
            params = _json.loads(args[args.index("--params") + 1])
            q = params.get("q", "")
            name = q.split("name = '")[1].split("'")[0] if "name = '" in q else ""
            hits = [{"id": did, "name": n} for (n, p, did) in folders if n == name]
            return {"files": hits}
        if args[1:3] == ("files", "create"):
            import json as _json
            body = _json.loads(args[args.index("--json") + 1])
            did = "new-" + body["name"]
            return {"id": did, "name": body["name"]}
        if args[1] == "+upload":
            return {"id": "up-1", "name": args[2].split("/")[-1]}
        raise AssertionError("unexpected cli call: %s" % (args,))

    monkeypatch.setattr(drive, "_cli", fake_cli)
    return calls


def test_disabled_when_cli_not_connected(monkeypatch):
    _mock_cli(monkeypatch, connected=False)
    d = DriveArchive()
    assert d.enabled is False
    h = d.health()
    assert h["component"] == "drive"
    assert h["status"] == "not_configured"


def test_folder_tree_matches_spec_section_26():
    assert ROOT_FOLDER == "AETHER_EARTH_INTELLIGENCE"
    assert ARCHIVE_FOLDERS == [
        "RAW", "PROCESSED", "TRAINING_DATA", "MODELS", "VALIDATION",
        "ALERT_ARCHIVE", "REPORTS", "BACKUPS",
    ]
    assert RAW_CATEGORIES == ["WEATHER", "EARTHQUAKE", "TSUNAMI", "SATELLITE", "RADAR"]


def test_root_id_from_bootstrap_file(monkeypatch, tmp_path):
    _mock_cli(monkeypatch, connected=True)
    (tmp_path / "DRIVE_ROOT_ID.txt").write_text("root-abc\n")
    assert resolve_root_id() == "root-abc"


def test_ensure_folders_creates_missing_tree(monkeypatch):
    _mock_cli(monkeypatch, connected=True, folders=())
    monkeypatch.setenv("AETHER_DRIVE_ROOT_FOLDER_ID", "root-1")
    res = DriveArchive().ensure_folders()
    assert res[ROOT_FOLDER] == "exists"
    assert res["%s/RAW" % ROOT_FOLDER] == "created"
    assert res["%s/BACKUPS" % ROOT_FOLDER] == "created"
    assert res["%s/RAW/WEATHER" % ROOT_FOLDER] == "created"
    assert len([k for k in res if not k.startswith("_")]) == 1 + 8 + 5


def test_ensure_folders_detects_existing(monkeypatch):
    folders = [("RAW", "root-1", "raw-1"), ("BACKUPS", "root-1", "bk-1"),
               ("WEATHER", "raw-1", "w-1")]
    _mock_cli(monkeypatch, connected=True, folders=folders)
    monkeypatch.setenv("AETHER_DRIVE_ROOT_FOLDER_ID", "root-1")
    res = DriveArchive().ensure_folders()
    assert res["%s/RAW" % ROOT_FOLDER] == "exists"
    assert res["%s/RAW/WEATHER" % ROOT_FOLDER] == "exists"
    assert res["%s/PROCESSED" % ROOT_FOLDER] == "created"


def test_upload_rejects_unknown_category(monkeypatch, tmp_path):
    _mock_cli(monkeypatch, connected=True)
    f = tmp_path / "x.json"
    f.write_text("{}")
    with pytest.raises(ValueError):
        DriveArchive().upload_parquet_or_json(str(f), "NOPE")


def test_upload_missing_file(monkeypatch):
    _mock_cli(monkeypatch, connected=True)
    res = DriveArchive().upload_parquet_or_json("/nonexistent/f.json", "WEATHER")
    assert res["status"] == "error"


def test_upload_ok(monkeypatch, tmp_path):
    folders = [("RAW", "root-1", "raw-1"), ("WEATHER", "raw-1", "w-1")]
    _mock_cli(monkeypatch, connected=True, folders=folders)
    monkeypatch.setenv("AETHER_DRIVE_ROOT_FOLDER_ID", "root-1")
    f = tmp_path / "sample.json"
    f.write_text("{}")
    res = DriveArchive().upload_parquet_or_json(str(f), "weather")
    assert res["status"] == "ok"
    assert res["category"] == "WEATHER"


def test_health_ok_when_connected(monkeypatch):
    _mock_cli(monkeypatch, connected=True)
    h = DriveArchive().health()
    assert h["status"] == "ok"


def _mock_cli_backups(monkeypatch, backups):
    """Fake CLI for the compressed-upload + prune path.

    backups: list of dicts with id/name/size/createdTime served for the
    BACKUPS parent listing. Records uploads and deletes.
    """
    import json as _json

    state = {"uploads": [], "deleted": []}

    def fake_cli(*args, **kwargs):
        if args[:2] == ("drive", "status"):
            return {"status": "connected"}
        if args[1] == "+upload":
            state["uploads"].append(args)
            with open(args[2], "rb") as fh:  # capture bytes at call time:
                state["uploaded_bytes"] = fh.read()  # temp file is cleaned up
            name = args[args.index("--name") + 1] if "--name" in args else "x"
            return {"id": "up-gz", "name": name}
        if args[1:3] == ("files", "list"):
            params = _json.loads(args[args.index("--params") + 1])
            if "in parents" in params.get("q", ""):
                return {"files": list(backups)}
            return {"files": []}
        if args[1:3] == ("files", "delete"):
            params = _json.loads(args[args.index("--params") + 1])
            state["deleted"].append(params["fileId"])
            return {}
        raise AssertionError("unexpected cli call: %s" % (args,))

    monkeypatch.setattr(drive, "_cli", fake_cli)
    return state


def test_upload_backup_compressed_ok(monkeypatch, tmp_path):
    state = _mock_cli_backups(monkeypatch, backups=[])
    d = DriveArchive()
    d._folders = {"BACKUPS": "bid"}
    src = tmp_path / "aether-test.db"
    src.write_bytes(b"SQLite format 3\x00" + b"x" * 5000)
    res = d.upload_backup_compressed(str(src), target_name="aether-test.db")
    assert res["status"] == "ok"
    assert res["file"] == "aether-test.db.gz"
    assert res["original_bytes"] == src.stat().st_size
    assert res["compressed_bytes"] < res["original_bytes"]
    # the uploaded payload must be a real gzip of the source
    import gzip
    assert gzip.decompress(state["uploaded_bytes"]) == src.read_bytes()


def test_prune_backups_keeps_newest(monkeypatch):
    backups = [
        {"id": "b%d" % i, "name": "aether-%02d.db.gz" % i, "size": str(100 * (i + 1)),
         "createdTime": "2026-10-%02dT00:00:00Z" % (i + 1)}
        for i in range(5)
    ]
    state = _mock_cli_backups(monkeypatch, backups=backups)
    d = DriveArchive()
    d._folders = {"BACKUPS": "bid"}
    res = d.prune_backups(keep=2)
    assert res["status"] == "ok"
    assert res["kept"] == 2
    assert res["deleted"] == 3
    assert res["errors"] == 0
    # the three OLDEST are deleted, newest two survive
    assert sorted(state["deleted"]) == ["b0", "b1", "b2"]
    assert res["freed_bytes"] == 100 + 200 + 300


def test_prune_backups_size_guard(monkeypatch):
    # 10 backups of 100 bytes each; cap kept total at 250 bytes with the
    # class min-keep patched to 2 -> keep newest 3 (300 > 250 at i=2),
    # delete the other 7, even though keep=48 would retain all.
    backups = [
        {"id": "s%d" % i, "name": "aether-%02d.db.gz" % i, "size": "100",
         "createdTime": "2026-10-%02dT00:00:00Z" % (i + 1)}
        for i in range(10)
    ]
    state = _mock_cli_backups(monkeypatch, backups=backups)
    monkeypatch.setattr(DriveArchive, "BACKUP_MAX_TOTAL_BYTES", 250)
    monkeypatch.setattr(DriveArchive, "BACKUP_MIN_KEEP", 2)
    d = DriveArchive()
    d._folders = {"BACKUPS": "bid"}
    res = d.prune_backups(keep=48)
    assert res["kept"] == 3
    assert res["deleted"] == 7
    assert sorted(state["deleted"]) == ["s0", "s1", "s2", "s3", "s4", "s5", "s6"]
