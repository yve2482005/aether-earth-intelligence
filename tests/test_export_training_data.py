"""Tests for scripts/export_training_data.py (Drive CLI fully mocked).

No live Google calls: backend.integrations.drive._cli is replaced with a
stateful fake Drive (folders + files), following tests/test_drive.py.
"""

import csv
import gzip
import importlib.util
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from backend.integrations import drive  # noqa: E402
from backend.integrations.drive import DriveArchive  # noqa: E402
from ml.weather.dataset import build_dataset  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "export_training_data", ROOT / "scripts" / "export_training_data.py")
export_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(export_mod)

TODAY = datetime(2026, 10, 17, tzinfo=timezone.utc)
EXPECTED_COLUMNS = [
    "valid_time", "location_id", "regime",
    "f_temperature", "f_humidity", "f_pressure", "f_wind_speed",
    "f_precipitation", "f_cloud_cover", "forecast_hour", "hour_of_day",
    "model_gfs", "model_icon_seamless", "model_ecmwf_ifs",
    "model_jma_seamless", "model_other",
    "regime_clear", "regime_tropical_convection", "regime_monsoon",
    "regime_heavy_rain", "regime_thunderstorm",
    "target_temperature", "target_rain_prob", "target_precip_amount",
    "target_wind", "target_pressure", "target_humidity",
]


@pytest.fixture(autouse=True)
def _reset_drive(monkeypatch, tmp_path):
    monkeypatch.setenv("AETHER_DRIVE_ROOT_FOLDER_ID", "root-1")
    monkeypatch.setattr(drive, "_connected_cache", None)
    monkeypatch.setattr(drive, "BOOTSTRAP_ROOT_ID", tmp_path / "DRIVE_ROOT_ID.txt")
    yield
    monkeypatch.setattr(drive, "_connected_cache", None)


def _mock_cli(monkeypatch, connected=True):
    """Stateful fake Drive. Returns state dict for assertions."""
    state = {"folders": {}, "files": {}, "uploads": [], "counter": 0}

    def fake_cli(*args, **kwargs):
        if args[:2] == ("drive", "status"):
            return {"status": "connected" if connected else "not_connected"}
        if args[1:3] == ("files", "list"):
            params = json.loads(args[args.index("--params") + 1])
            q = params.get("q", "")
            if "name = '" in q:  # folder lookup by name
                name = q.split("name = '")[1].split("'")[0]
                if name in state["folders"]:
                    return {"files": [{"id": state["folders"][name], "name": name}]}
                return {"files": []}
            parent = q.split("'")[1]  # '<id>' in parents ...
            return {"files": list(state["files"].get(parent, []))}
        if args[1:3] == ("files", "create"):
            body = json.loads(args[args.index("--json") + 1])
            fid = "folder-" + body["name"]
            state["folders"][body["name"]] = fid
            return {"id": fid, "name": body["name"]}
        if args[1] == "+upload":
            state["counter"] += 1
            parent = args[args.index("--parent") + 1]
            if "--name" in args:
                name = args[args.index("--name") + 1]
            else:
                name = args[2].split("/")[-1]
            fid = "up-%d" % state["counter"]
            state["files"].setdefault(parent, []).append({"id": fid, "name": name})
            state["uploads"].append({"id": fid, "name": name, "parent": parent})
            return {"id": fid, "name": name}
        raise AssertionError("unexpected cli call: %s" % (args,))

    monkeypatch.setattr(drive, "_cli", fake_cli)
    return state


def _run_export(db, tmp_path, archive=None):
    return export_mod.export_training_data(
        db_path=db,
        export_dir=tmp_path / "exports",
        registry_path=tmp_path / "training_registry.csv",
        archive=archive if archive is not None else DriveArchive(),
        today=TODAY,
    )


def _registry_rows(tmp_path):
    with (tmp_path / "training_registry.csv").open(newline="") as fh:
        return list(csv.DictReader(fh))


def test_export_uploads_valid_gz_csv(populated_weather_db, monkeypatch, tmp_path):
    state = _mock_cli(monkeypatch, connected=True)
    expected_pairs = build_dataset(populated_weather_db).n_pairs
    assert expected_pairs > 0

    summary = _run_export(populated_weather_db, tmp_path)

    assert summary["status"] == "uploaded"
    assert summary["dataset_version"].startswith("ds_")
    assert summary["pairs"] == expected_pairs
    assert summary["locations_count"] == 1
    assert summary["period_start"] <= summary["period_end"]

    # the local gz CSV exists, is valid gzip CSV, expected schema + row count
    local = Path(summary["local_path"])
    assert local.name == f"training_pairs_{summary['dataset_version']}_20261017.csv.gz"
    assert summary["file_bytes"] == local.stat().st_size > 0
    with gzip.open(local, "rt", newline="") as fh:
        rows = list(csv.reader(fh))
    assert rows[0] == EXPECTED_COLUMNS
    assert len(rows) - 1 == expected_pairs
    assert len(rows[1]) == len(EXPECTED_COLUMNS)

    # exactly one Drive upload, into the TRAINING_DATA folder, with the name
    assert len(state["uploads"]) == 1
    td_id = state["folders"]["TRAINING_DATA"]
    assert state["uploads"][0]["parent"] == td_id
    assert state["uploads"][0]["name"] == local.name
    assert summary["drive"]["drive_id"] == state["uploads"][0]["id"]

    # registry row appended with the same facts
    reg = _registry_rows(tmp_path)
    assert len(reg) == 1
    assert reg[0]["dataset_version"] == summary["dataset_version"]
    assert reg[0]["pair_rows"] == str(expected_pairs)
    assert reg[0]["period_start"] == summary["period_start"]
    assert reg[0]["period_end"] == summary["period_end"]
    assert reg[0]["locations_count"] == "1"
    assert reg[0]["drive_file_name"] == local.name
    assert reg[0]["drive_file_id"] == state["uploads"][0]["id"]


def test_export_skips_version_already_in_drive(populated_weather_db, monkeypatch, tmp_path):
    state = _mock_cli(monkeypatch, connected=True)
    archive = DriveArchive()

    first = _run_export(populated_weather_db, tmp_path, archive=archive)
    assert first["status"] == "uploaded"
    assert len(state["uploads"]) == 1

    second = _run_export(populated_weather_db, tmp_path, archive=archive)
    assert second["status"] == "skipped"
    assert second["drive"]["reason"] == "version already stored"
    assert second["dataset_version"] == first["dataset_version"]
    assert second["drive"]["drive_id"] == state["uploads"][0]["id"]
    # no second upload happened; registry still records the (skipped) run
    assert len(state["uploads"]) == 1
    assert len(_registry_rows(tmp_path)) == 2


def test_export_drive_not_configured_still_writes_local(populated_weather_db,
                                                        monkeypatch, tmp_path):
    _mock_cli(monkeypatch, connected=False)
    summary = _run_export(populated_weather_db, tmp_path)
    assert summary["status"] == "not_configured"
    assert Path(summary["local_path"]).is_file()
    reg = _registry_rows(tmp_path)
    assert len(reg) == 1
    assert reg[0]["drive_file_id"] == ""
