"""Registry tests: version format, champion management, deploy gate.

The deploy gate must only promote a candidate that is strictly better than
the champion on the task's predefined criterion (MAE for regression, Brier
for rain probability), and synthetic smoke-test models must never deploy.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.ml_support import registry  # noqa: E402

SCHEMA = (ROOT / "backend" / "db" / "schema.sql").read_text()


@pytest.fixture()
def reg_db(tmp_path):
    db = str(tmp_path / "reg.db")
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA)
    conn.close()
    yield db


def _mk(db, version, task, metrics, champion=False):
    return registry.register_model_version(
        db, version=version, task=task, dataset_version="ds_x",
        feature_version="v1", metrics=metrics,
        training_start="2026-09-01T00:00:00Z",
        training_end="2026-09-20T00:00:00Z",
        validation_start="2026-09-20T00:00:00Z",
        validation_end="2026-09-25T00:00:00Z",
        is_champion=champion)


def test_version_format_enforced(reg_db):
    with pytest.raises(ValueError, match="bad model version"):
        _mk(reg_db, "temperature_latest", "temperature", {"mae": 1.0})
    with pytest.raises(ValueError, match="does not match"):
        _mk(reg_db, "weather_wind_v2026_10_05_001", "temperature",
             {"mae": 1.0})
    v = _mk(reg_db, "weather_temperature_v2026_10_05_001", "temperature",
            {"mae": 1.0})
    assert v == "weather_temperature_v2026_10_05_001"


def test_next_version_increments(reg_db):
    v1 = registry.next_version("temperature", reg_db,
                               when="2026-10-05T00:00:00Z")
    assert v1 == "weather_temperature_v2026_10_05_001"
    _mk(reg_db, v1, "temperature", {"mae": 2.0})
    v2 = registry.next_version("temperature", reg_db,
                               when="2026-10-05T00:00:00Z")
    assert v2 == "weather_temperature_v2026_10_05_002"


def test_deploy_gate_first_model_becomes_champion(reg_db):
    v = _mk(reg_db, "weather_temperature_v2026_10_05_001", "temperature",
            {"mae": 1.5})
    run = registry.record_training_run(
        reg_db, run_id="r1", task="temperature", dataset_version="ds_x",
        model_version=v, rows=100, metrics={"mae": 1.5}, notes="")
    gate = registry.deploy_gate(reg_db, task="temperature",
                                candidate_version=v,
                                candidate_metrics={"mae": 1.5}, run_id="r1")
    assert gate["deploy"] is True
    assert gate["criterion"] == "mae"
    champ = registry.get_champion(reg_db, "temperature")
    assert champ["version"] == v
    conn = sqlite3.connect(reg_db)
    notes = conn.execute(
        "SELECT notes FROM training_runs WHERE run_id='r1'").fetchone()[0]
    conn.close()
    assert "deploy_gate decision: DEPLOY" in notes


def test_deploy_gate_rejects_worse_candidate(reg_db):
    champ_v = _mk(reg_db, "weather_temperature_v2026_10_05_001",
                  "temperature", {"mae": 1.0}, champion=True)
    cand_v = _mk(reg_db, "weather_temperature_v2026_10_05_002",
                 "temperature", {"mae": 1.8})
    registry.record_training_run(
        reg_db, run_id="r2", task="temperature", dataset_version="ds_x",
        model_version=cand_v, rows=100, metrics={"mae": 1.8}, notes="")
    gate = registry.deploy_gate(reg_db, task="temperature",
                                candidate_version=cand_v,
                                candidate_metrics={"mae": 1.8}, run_id="r2")
    assert gate["deploy"] is False
    assert "not better" in gate["reason"]
    assert registry.get_champion(reg_db, "temperature")["version"] == champ_v


def test_deploy_gate_uses_brier_for_rain_prob(reg_db):
    champ_v = _mk(reg_db, "weather_rain_prob_v2026_10_05_001", "rain_prob",
                  {"brier": 0.20}, champion=True)
    cand_v = _mk(reg_db, "weather_rain_prob_v2026_10_05_002", "rain_prob",
                 {"brier": 0.18})
    registry.record_training_run(
        reg_db, run_id="r3", task="rain_prob", dataset_version="ds_x",
        model_version=cand_v, rows=100, metrics={"brier": 0.18}, notes="")
    gate = registry.deploy_gate(reg_db, task="rain_prob",
                                candidate_version=cand_v,
                                candidate_metrics={"brier": 0.18}, run_id="r3")
    assert gate["deploy"] is True
    assert gate["criterion"] == "brier"
    assert registry.get_champion(reg_db, "rain_prob")["version"] == cand_v


def test_deploy_gate_never_deploys_synthetic(reg_db):
    v = _mk(reg_db, "weather_temperature_v2026_10_05_001", "temperature",
            {"mae": 0.01, "data_kind": "synthetic_smoke"})
    registry.record_training_run(
        reg_db, run_id="rs", task="temperature", dataset_version="ds_x",
        model_version=v, rows=100,
        metrics={"mae": 0.01, "data_kind": "synthetic_smoke"},
        notes="synthetic smoke test — not a real model")
    gate = registry.deploy_gate(
        reg_db, task="temperature", candidate_version=v,
        candidate_metrics={"mae": 0.01, "data_kind": "synthetic_smoke"},
        run_id="rs")
    assert gate["deploy"] is False
    assert "synthetic" in gate["reason"]
    assert registry.get_champion(reg_db, "temperature") is None


def test_record_model_scores_roundtrip(reg_db):
    n = registry.record_model_scores(reg_db, [{
        "location_id": None, "variable": "temperature", "horizon": 12,
        "model": "weather_temperature_v2026_10_05_001",
        "mae": 1.2, "rmse": 1.6, "sample_count": 50,
        "period_start": "2026-09-20T00:00:00Z",
        "period_end": "2026-09-25T00:00:00Z",
        "model_version": "weather_temperature_v2026_10_05_001"}])
    assert n == 1
    conn = sqlite3.connect(reg_db)
    row = conn.execute("SELECT mae, rmse FROM model_scores").fetchone()
    conn.close()
    assert row[0] == pytest.approx(1.2)
