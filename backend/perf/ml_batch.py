"""Batch ML inference (§§20-21): batch feature assembly → single predict call per model.

Rules honored:
  - NEVER train on the request path (§55). This module only loads existing
    model artifacts and predicts. Training stays in the offline training
    pipeline (slow path).
  - `precompute_predictions()` is the scheduled-job entry point: it assembles
    one feature matrix for all pending forecast rows and calls predict ONCE
    per model/estimator — not once per row.
  - Model selection: the champion registered in `model_versions`
    (is_champion=1) for the task, resolved through the caller's DB
    connection. Only when no champion registry is reachable (e.g. a
    throwaway test DB) does it fall back to the _TASK_TO_FILE defaults,
    which name the current real-data champions (v2026_10_17_001, trained
    from scratch on worldwide pairs — see that version's MODEL_CARD.md).
    Every stored prediction is labeled with the exact model version +
    data kind that produced it.
  - Inference mirrors the trainer's semantics (ml/weather/train.py):
    probability tasks use predict_proba + the artifact's fitted
    per-regime calibrators; a location with neither a per-location
    estimator nor a "global" fallback raises instead of silently borrowing
    another location's model.
  - Results are stored in `ml_batch_predictions` for the API to serve;
    nothing on the hot path blocks on inference.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import joblib
import numpy as np

from backend.common.logging_setup import setup_logging
from backend.common.timeutil import utc_iso
from backend.perf import metrics as metrics_mod

log = setup_logging("aether.perf.ml_batch")

MODELS_DIR = Path(__file__).resolve().parents[2] / "models"
_TASK_TO_FILE = {
    "temperature": "weather_temperature_v2026_10_17_001",
    "rain_prob": "weather_rain_prob_v2026_10_17_001",
    "precip_amount": "weather_precip_amount_v2026_10_17_001",
    "wind": "weather_wind_v2026_10_17_001",
    "pressure": "weather_pressure_v2026_10_17_001",
    "humidity": "weather_humidity_v2026_10_17_001",
}

_artifact_cache: Dict[str, Dict[str, Any]] = {}


def champion_version(conn: sqlite3.Connection, task: str) -> Optional[str]:
    """Registered champion version for `task` from model_versions, or None.

    Returns None when the registry table is missing/unreachable or no
    champion is flagged — callers then fall back to the bundled artifacts.
    """
    try:
        row = conn.execute(
            "SELECT version FROM model_versions WHERE task=? AND is_champion=1"
            " ORDER BY deployed_at DESC LIMIT 1", (task,)).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return row[0] if not hasattr(row, "keys") else row["version"]


def load_artifact(task: str, version: Optional[str] = None) -> Dict[str, Any]:
    """Load (and cache) the joblib artifact for a task. Never trains.

    `version` is a model directory name (e.g. a champion version from
    model_versions). Without it, the bundled v2026_10_17_001 artifact is
    loaded (synthetic-validated fallback; see module docstring).
    """
    name = version or _TASK_TO_FILE[task]
    if name in _artifact_cache:
        return _artifact_cache[name]
    art = joblib.load(str(MODELS_DIR / name / "artifact.joblib"))
    _artifact_cache[name] = art
    return art


_KNOWN_MODELS = ("gfs", "icon_seamless", "ecmwf_ifs", "jma_seamless")


def _hour_of_day(valid_time: Optional[str]) -> float:
    """UTC hour-of-day (fractional) from an ISO valid_time, as in training."""
    if not valid_time:
        return 12.0
    try:
        from datetime import datetime

        dt = datetime.fromisoformat(str(valid_time).replace("Z", "+00:00"))
        return float(dt.hour) + float(dt.minute) / 60.0
    except ValueError:
        return 12.0


def feature_row(forecast: Dict[str, Any], feature_names: List[str]) -> List[float]:
    """Map a weather_forecasts row to the model's feature vector.

    Mirrors ml/weather/dataset.py exactly: hour_of_day from valid_time,
    model one-hot with model_other for unseen models, and the weather
    regime from the trainer's own classify_regime().
    """
    from ml.weather.dataset import REGIMES, classify_regime

    model = forecast.get("model")
    regime = classify_regime(forecast.get("cloud_cover"),
                             forecast.get("precipitation"),
                             forecast.get("precip_probability"))
    mapping = {
        "f_temperature": forecast.get("temperature"),
        "f_humidity": forecast.get("humidity"),
        "f_pressure": forecast.get("pressure"),
        "f_wind_speed": forecast.get("wind_speed"),
        "f_precipitation": forecast.get("precipitation"),
        "f_cloud_cover": forecast.get("cloud_cover"),
        "forecast_hour": forecast.get("forecast_hour"),
        "hour_of_day": _hour_of_day(forecast.get("valid_time")),
        "model_other": 0.0 if model in _KNOWN_MODELS else 1.0,
    }
    for m in _KNOWN_MODELS:
        mapping[f"model_{m}"] = 1.0 if model == m else 0.0
    for r in REGIMES:
        mapping[f"regime_{r}"] = 1.0 if regime == r else 0.0
    return [float(mapping.get(f, 0.0) or 0.0) for f in feature_names]


def _estimator_predict(art: Dict[str, Any], est: Any, X: np.ndarray) -> np.ndarray:
    """One estimator's raw predictions, using trainer semantics."""
    if art.get("kind") == "probability":
        return np.asarray(est.predict_proba(X)[:, 1], dtype=float)
    return np.asarray(est.predict(X), dtype=float)


def _row_regimes(art: Dict[str, Any], X: np.ndarray) -> List[str]:
    """Decode each row's regime from its one-hot columns (for calibrators)."""
    names = art["feature_names"]
    cols = [(i, n[len("regime_"):]) for i, n in enumerate(names)
            if n.startswith("regime_")]
    regimes: List[str] = []
    for row in X:
        regime = "clear"
        for i, name in cols:
            if row[i] > 0.0:
                regime = name
                break
        regimes.append(regime)
    return regimes


def predict_batch(task: str, feature_matrix: np.ndarray,
                  location_ids: Optional[List[int]] = None,
                  version: Optional[str] = None) -> np.ndarray:
    """Single predict call per estimator over the whole batch.

    Returns one prediction per row, in order. One predict() call total per
    estimator — N rows cost ~1 model invocation. Probability tasks return
    calibrated probabilities (predict_proba + the artifact's fitted
    per-regime calibrators), exactly as the trainer evaluates them.

    Location routing: a row uses its own location's estimator when one
    exists, else the artifact's "global" fallback. A row with neither
    raises ValueError — another location's model is never silently used.
    """
    art = load_artifact(task, version)
    feature_names = art["feature_names"]
    if feature_matrix.shape[1] != len(feature_names):
        raise ValueError(
            f"feature matrix has {feature_matrix.shape[1]} cols, "
            f"model expects {len(feature_names)}")
    estimators = art["estimators"]
    out = np.empty(feature_matrix.shape[0], dtype=float)
    if isinstance(estimators, dict):
        if location_ids is None:
            location_ids = [None] * feature_matrix.shape[0]
        groups: Dict[Any, List[int]] = {}
        uncovered: List[Any] = []
        for i, lid in enumerate(location_ids):
            if lid in estimators:
                k = lid
            elif str(lid) in estimators:
                k = str(lid)
            elif "global" in estimators:
                k = "global"
            else:
                uncovered.append(lid)
                continue
            groups.setdefault(k, []).append(i)
        if uncovered:
            raise ValueError(
                f"no estimator for locations {sorted(set(uncovered))} and "
                f"artifact has no global fallback; refusing to borrow "
                f"another location's model")
        for k, idxs in groups.items():
            out[idxs] = _estimator_predict(art, estimators[k],
                                           feature_matrix[idxs])
    else:
        out = _estimator_predict(art, estimators, feature_matrix)
    if art.get("kind") == "probability" and art.get("calibrators"):
        from ml.weather.calibrate import apply_calibrators

        out = apply_calibrators(art["calibrators"], out,
                                _row_regimes(art, feature_matrix))
    return out


def ensure_predictions_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS ml_batch_predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task TEXT NOT NULL,
            model_version TEXT NOT NULL,
            location_id INTEGER,
            valid_time TEXT NOT NULL,
            prediction REAL NOT NULL,
            features_hash TEXT,
            predicted_at TEXT NOT NULL,
            UNIQUE(task, model_version, location_id, valid_time)
        )""")
    conn.commit()


def precompute_predictions(conn: sqlite3.Connection,
                           task: str = "temperature",
                           limit: int = 500,
                           location_id: Optional[int] = None,
                           min_valid_time: Optional[str] = None) -> Dict[str, Any]:
    """Scheduled-job entry point: batch-predict the next `limit` forecast rows.

    Never trains. Uses the registered champion for the task when the
    caller's DB has one (see champion_version); otherwise the bundled
    synthetic-validated artifact. `location_id` / `min_valid_time`
    optionally scope the batch (e.g. one location, future valid times
    only). Predictions are deduplicated per (task, version, location,
    valid_time) — when several forecast models share a valid_time, the
    first row's features produce the stored prediction.
    """
    t0 = time.monotonic()
    version = champion_version(conn, task) or _TASK_TO_FILE[task]
    art = load_artifact(task, version)
    data_kind = art.get("data_kind", "unknown")
    ensure_predictions_table(conn)
    where = ["p.id IS NULL"]
    params: List[Any] = [task, version]
    if location_id is not None:
        where.append("f.location_id=?")
        params.append(location_id)
    if min_valid_time is not None:
        where.append("f.valid_time>=?")
        params.append(min_valid_time)
    params.append(limit)
    rows = [dict(r) for r in conn.execute(
        """SELECT f.location_id, f.valid_time, f.temperature, f.humidity, f.pressure,
                  f.wind_speed, f.precipitation, f.precip_probability, f.cloud_cover,
                  f.forecast_hour, f.model
           FROM weather_forecasts f
           LEFT JOIN ml_batch_predictions p
             ON p.task=? AND p.model_version=? AND p.location_id=f.location_id
                AND p.valid_time=f.valid_time
           WHERE """ + " AND ".join(where) + """
           GROUP BY f.location_id, f.valid_time
           ORDER BY f.valid_time ASC LIMIT ?""",
        params)]
    if not rows:
        return {"task": task, "version": version, "predicted": 0,
                "data_kind": data_kind, "latency_ms": (time.monotonic() - t0) * 1000.0}
    X = np.asarray([feature_row(r, art["feature_names"]) for r in rows])
    preds = predict_batch(task, X, [r["location_id"] for r in rows],
                          version=version)
    now = utc_iso()
    # executemany (changed 2026-10-18): the old per-row execute loop held
    # the SQLite write lock for the whole batch; under disk load that
    # starved the 5-minute collectors (same failure pattern fixed in the
    # weather stores). One executemany keeps INSERT OR IGNORE semantics
    # (rowcount = rows actually inserted) with a far shorter lock hold.
    cur = conn.executemany(
        """INSERT OR IGNORE INTO ml_batch_predictions
           (task, model_version, location_id, valid_time, prediction, predicted_at)
           VALUES (?,?,?,?,?,?)""",
        [(task, version, r["location_id"], r["valid_time"], float(p), now)
         for r, p in zip(rows, preds)])
    n = cur.rowcount if cur.rowcount is not None else 0
    conn.commit()
    latency_ms = (time.monotonic() - t0) * 1000.0
    metrics_mod.record("model", f"batch_predict:{task}", latency_ms)
    if data_kind == "real":
        caveat = ("champion model trained on real forecast/observation "
                  "pairs; predictions are estimates, not guarantees")
    else:
        caveat = ("model validated on synthetic data only; "
                  "not a real-world guarantee")
    return {"task": task, "version": version, "predicted": n,
            "data_kind": data_kind, "latency_ms": latency_ms,
            "caveat": caveat}
