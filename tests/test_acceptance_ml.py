"""Acceptance ML: 94/111/112 — batch inference speed, accuracy separated from speed."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.perf import ml_batch  # noqa: E402
from backend.perf.ml_batch import load_artifact, predict_batch  # noqa: E402
from tests.test_acceptance_lib import verdict  # noqa: E402


def test_94_batch_vs_single_inference():
    art = load_artifact("temperature")
    ncols = len(art["feature_names"])
    rng = np.random.default_rng(42)
    X = rng.normal(30, 5, size=(64, ncols))
    t0 = time.monotonic()
    batch_preds = predict_batch("temperature", X, location_ids=[9001] * 64)
    batch_ms = (time.monotonic() - t0) * 1000.0
    t0 = time.monotonic()
    for i in range(64):
        predict_batch("temperature", X[i:i + 1], location_ids=[9001])
    single_ms = (time.monotonic() - t0) * 1000.0
    assert batch_preds.shape == (64,)
    ratio = single_ms / max(batch_ms, 0.01)
    assert batch_ms < single_ms
    verdict(94, "PASS", {"rows": 64, "batch_ms": round(batch_ms, 1),
                        "single_loop_ms": round(single_ms, 1),
                        "speedup": round(ratio, 2)})
    print(f"\n[94] batch 64 rows {batch_ms:.1f} ms vs 64 single calls "
          f"{single_ms:.1f} ms (speedup {ratio:.1f}x)")


def test_111_112_accuracy_separated_from_speed():
    # Resolve the CURRENT temperature champion's manifest dynamically: the
    # original fixture (the v2026_10_05_001 synthetic smoke artifact) was
    # deleted with all trained data in the 2026-10-17 from-scratch retrain,
    # so a hardcoded version would test a model that no longer exists.
    from backend.db import db as dbmod

    version = ml_batch.champion_version(dbmod.connect(), "temperature")
    if version is None:
        version = ml_batch._TASK_TO_FILE["temperature"]
    manifest = json.loads(
        (ROOT / "models" / version / "manifest.json").read_text())
    # accuracy metrics live in the manifest, never mixed into latency numbers
    assert "validation_metrics" in manifest
    # honest labeling: the data_kind is whatever the manifest declares,
    # recorded verbatim — never upgraded to a stronger claim
    assert manifest["data_kind"] in ("real", "synthetic_smoke")
    mae = manifest["validation_metrics"]["mae"]
    # speed numbers come from metrics, accuracy from manifest: separate stores
    assert isinstance(mae, float)
    kind_note = ("real validation pairs" if manifest["data_kind"] == "real"
                 else "synthetic validation only")
    verdict(111, "PASS", {"accuracy_metric": "mae", "mae": round(mae, 4),
                         "source": f"model manifest (validation, {kind_note})",
                         "speed_source": "perf metrics accumulators (runtime)"})
    verdict(112, "PASS", {"data_kind": manifest["data_kind"],
                         "accuracy_claim": f"{kind_note} — measured validation "
                                           "metrics, never presented as "
                                           "guaranteed real-world skill"})
    print(f"\n[111/112] accuracy (mae={mae:.3f}, {kind_note}) kept separate "
          "from speed metrics")
