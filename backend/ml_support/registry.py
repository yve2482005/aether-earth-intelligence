"""Model registry: model_versions / model_scores / training_runs read+write.

Owned by the ML team (per CONTRACTS.md). Backend team owns schema.sql.

Honesty rules enforced here:
  * Metrics are reported per task (MAE/RMSE/Brier/CRPS/calibration error);
    never "accuracy %" claims and never certainty language.
  * The deploy gate only promotes a candidate when it is strictly better than
    the current champion on the task's predefined criterion. The decision and
    its evidence are recorded in training_runs.notes.
  * Synthetic smoke-test runs are never marked champion and never deployed.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone

# version format: weather_{task}_vYYYY_MM_DD_NNN
_VERSION_RE = re.compile(
    r"^weather_(temperature|rain_prob|precip_amount|wind|pressure|humidity)"
    r"_v(\d{4})_(\d{2})_(\d{2})_(\d{3})$"
)

# Deploy-gate criterion per task: metric name + "lower is better".
# rain_prob is a probability task -> Brier score; everything else -> MAE.
GATE_CRITERION = {
    "temperature": "mae",
    "rain_prob": "brier",
    "precip_amount": "mae",
    "wind": "mae",
    "pressure": "mae",
    "humidity": "mae",
}

# Sanity floors for the deploy gate (added 2026-10-21 after degenerate
# candidates with n=654 and near-zero error were promoted over real
# champions): a candidate must be validated on at least
# GATE_MIN_VALIDATION_N rows (when n is reported) and its criterion must
# be at or above a physically plausible floor. A real weather model never
# fits observations to ~1e-13 MAE; such values mean the dataset
# degenerated (e.g. trained on an already-consumed window), not that the
# model is perfect.
GATE_MIN_VALIDATION_N = 10_000
# A candidate whose validation window spans less than this is not
# evidence: a single-instant validation (span 0) produced a "perfect"
# rain_prob champion on 2026-10-21. Legit champions validate over ~5h+.
GATE_MIN_VALIDATION_SPAN_SECONDS = 3600
GATE_CRITERION_FLOOR = {
    "temperature": 0.1,
    "rain_prob": 0.001,
    "precip_amount": 0.001,
    "wind": 0.05,
    "pressure": 0.1,
    "humidity": 0.5,
}


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def validate_version(version: str) -> str:
    """Return the task embedded in the version string, or raise ValueError."""
    m = _VERSION_RE.match(version)
    if not m:
        raise ValueError(
            f"bad model version {version!r}: expected "
            "weather_{task}_vYYYY_MM_DD_NNN"
        )
    return m.group(1)


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def next_version(task: str, db_path: str, when: str | None = None) -> str:
    """Allocate the next version string for a task, e.g.
    weather_temperature_v2026_10_05_001. The sequence number is derived from
    existing rows so two runs on the same day do not collide."""
    when = when or utcnow()
    day = when[:10].replace("-", "_")
    prefix = f"weather_{task}_v{day}_"
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT version FROM model_versions WHERE version LIKE ?",
            (prefix + "%",),
        ).fetchall()
    seqs = [int(r["version"].rsplit("_", 1)[1]) for r in rows]
    nxt = max(seqs, default=0) + 1
    return f"{prefix}{nxt:03d}"


def register_model_version(
    db_path: str,
    *,
    version: str,
    task: str,
    dataset_version: str,
    feature_version: str,
    source_versions: str = "",
    training_start: str = "",
    training_end: str = "",
    validation_start: str = "",
    validation_end: str = "",
    metrics: dict | None = None,
    hyperparameters: dict | None = None,
    git_commit: str = "n/a",
    deployed_at: str | None = None,
    is_champion: bool = False,
) -> str:
    """Insert a model_versions row. Returns the version. Raises on bad format."""
    embedded = validate_version(version)
    if embedded != task:
        raise ValueError(
            f"version task {embedded!r} does not match requested task {task!r}"
        )
    with _connect(db_path) as conn:
        conn.execute(
            """INSERT INTO model_versions
               (version, task, dataset_version, feature_version, source_versions,
                training_start, training_end, validation_start, validation_end,
                metrics, hyperparameters, git_commit, deployed_at, is_champion)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                version, task, dataset_version, feature_version, source_versions,
                training_start, training_end, validation_start, validation_end,
                json.dumps(metrics or {}), json.dumps(hyperparameters or {}),
                git_commit, deployed_at, 1 if is_champion else 0,
            ),
        )
        conn.commit()
    return version


def record_training_run(
    db_path: str,
    *,
    run_id: str,
    task: str,
    dataset_version: str,
    model_version: str | None,
    rows: int,
    metrics: dict | None = None,
    status: str = "success",  # success|failed|rejected
    deployed: bool = False,
    notes: str = "",
    started_at: str | None = None,
    ended_at: str | None = None,
) -> str:
    if status not in ("success", "failed", "rejected"):
        raise ValueError(f"bad training run status {status!r}")
    with _connect(db_path) as conn:
        conn.execute(
            """INSERT INTO training_runs
               (run_id, task, dataset_version, model_version, started_at,
                ended_at, rows, metrics, status, deployed, notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id, task, dataset_version, model_version,
                started_at or utcnow(), ended_at or utcnow(), rows,
                json.dumps(metrics or {}), status, 1 if deployed else 0, notes,
            ),
        )
        conn.commit()
    return run_id


def record_model_scores(db_path: str, rows: list[dict]) -> int:
    """Insert model_scores rows. Each dict needs: variable, horizon, model,
    and optional mae/rmse/brier/crps/calibration_error/sample_count/
    period_start/period_end/model_version/location_id."""
    n = 0
    with _connect(db_path) as conn:
        for r in rows:
            conn.execute(
                """INSERT OR REPLACE INTO model_scores
                   (location_id, variable, horizon, model, mae, rmse, brier,
                    crps, calibration_error, sample_count, period_start,
                    period_end, model_version)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    r.get("location_id"), r["variable"], r["horizon"],
                    r["model"], r.get("mae"), r.get("rmse"), r.get("brier"),
                    r.get("crps"), r.get("calibration_error"),
                    r.get("sample_count"), r.get("period_start"),
                    r.get("period_end"), r.get("model_version"),
                ),
            )
            n += 1
        conn.commit()
    return n


def get_champion(db_path: str, task: str) -> dict | None:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM model_versions WHERE task=? AND is_champion=1",
            (task,),
        ).fetchone()
    return dict(row) if row else None


def set_champion(db_path: str, version: str) -> None:
    """Promote version to champion, demoting the previous champion of the
    same task. Does NOT decide whether it deserves promotion — use
    deploy_gate for that."""
    task = validate_version(version)
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE model_versions SET is_champion=0 WHERE task=?", (task,)
        )
        cur = conn.execute(
            "UPDATE model_versions SET is_champion=1, deployed_at=? "
            "WHERE version=?",
            (utcnow(), version),
        )
        if cur.rowcount == 0:
            raise ValueError(f"unknown model version {version!r}")
        conn.commit()


def _validation_span_seconds(db_path: str, version: str) -> float | None:
    """Seconds between the candidate's registered validation_start and
    validation_end, or None when unknown/unparseable."""
    try:
        with _connect(db_path) as conn:
            row = conn.execute(
                "SELECT validation_start, validation_end FROM model_versions "
                "WHERE version=?", (version,)).fetchone()
    except sqlite3.Error:
        return None
    if not row or not row["validation_start"] or not row["validation_end"]:
        return None
    try:
        start = datetime.strptime(row["validation_start"],
                                  "%Y-%m-%dT%H:%M:%SZ")
        end = datetime.strptime(row["validation_end"],
                                "%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError):
        return None
    return (end - start).total_seconds()


def deploy_gate(
    db_path: str,
    *,
    task: str,
    candidate_version: str,
    candidate_metrics: dict,
    run_id: str | None = None,
    allow_synthetic: bool = False,
) -> dict:
    """Decide whether the candidate may replace the champion.

    Criterion is predefined per task (lower MAE for regression, lower Brier
    for rain probability). The decision and its evidence are appended to
    training_runs.notes for the candidate's run. Returns
    {"deploy": bool, "reason": str, "criterion": str,
     "candidate": float|None, "champion": float|None}.
    """
    criterion = GATE_CRITERION[task]
    cand_val = candidate_metrics.get(criterion)
    champion = get_champion(db_path, task)
    champ_val = None
    if champion:
        try:
            champ_val = json.loads(champion["metrics"] or "{}").get(criterion)
        except (ValueError, TypeError):
            champ_val = None

    synthetic = "synthetic" in (candidate_metrics.get("data_kind") or "").lower()
    cand_n = candidate_metrics.get("n")
    cand_span = _validation_span_seconds(db_path, candidate_version)
    if synthetic and not allow_synthetic:
        deploy, reason = False, (
            "rejected: synthetic smoke-test model is never deployed "
            "and never marked champion"
        )
    elif cand_val is not None and cand_val < GATE_CRITERION_FLOOR[task]:
        deploy, reason = False, (
            f"rejected: candidate {criterion}={cand_val:.4g} is below the "
            f"plausibility floor {GATE_CRITERION_FLOOR[task]:.4g} for task "
            f"{task!r}; a near-zero error indicates a degenerate dataset, "
            "not a real improvement; keeping champion"
        )
    elif cand_n is not None and cand_n < GATE_MIN_VALIDATION_N:
        deploy, reason = False, (
            f"rejected: candidate validated on only n={cand_n} rows "
            f"(< {GATE_MIN_VALIDATION_N} minimum); too little evidence "
            "to replace a champion"
        )
    elif cand_span is not None and cand_span < GATE_MIN_VALIDATION_SPAN_SECONDS:
        deploy, reason = False, (
            f"rejected: candidate validation window spans only "
            f"{cand_span:.0f}s (< {GATE_MIN_VALIDATION_SPAN_SECONDS}s "
            "minimum); a single-instant validation is not evidence of "
            "skill; keeping champion"
        )
    elif cand_val is None:
        deploy, reason = False, (
            f"rejected: candidate metrics missing required {criterion!r}"
        )
    elif champion is None:
        deploy, reason = True, (
            f"deployed: no existing champion for task {task!r}; "
            f"candidate {criterion}={cand_val:.4g} becomes baseline"
        )
    elif champ_val is None:
        deploy, reason = False, (
            "rejected: champion metrics missing "
            f"{criterion!r}; cannot compare, keeping champion"
        )
    elif cand_val < champ_val:
        deploy = True
        reason = (
            f"deployed: candidate {criterion}={cand_val:.4g} is better than "
            f"champion {champion['version']} {criterion}={champ_val:.4g} "
            f"(lower is better)"
        )
    else:
        deploy, reason = False, (
            f"rejected: candidate {criterion}={cand_val:.4g} is not better "
            f"than champion {champion['version']} {criterion}={champ_val:.4g} "
            f"(lower is better)"
        )

    note = (
        f"deploy_gate decision: {'DEPLOY' if deploy else 'REJECT'}. {reason}"
    )
    if run_id:
        with _connect(db_path) as conn:
            cur = conn.execute(
                "SELECT notes FROM training_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if cur is not None:
                prev = cur["notes"] or ""
                conn.execute(
                    "UPDATE training_runs SET notes=?, deployed=? "
                    "WHERE run_id=?",
                    (prev + ("\n" if prev else "") + note,
                     1 if deploy else 0, run_id),
                )
                conn.commit()
    if deploy:
        set_champion(db_path, candidate_version)
    return {
        "deploy": deploy,
        "reason": reason,
        "criterion": criterion,
        "candidate": cand_val,
        "champion": champ_val,
        "champion_version": champion["version"] if champion else None,
    }


def list_versions(db_path: str, task: str | None = None) -> list[dict]:
    with _connect(db_path) as conn:
        if task:
            rows = conn.execute(
                "SELECT * FROM model_versions WHERE task=? ORDER BY version",
                (task,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM model_versions ORDER BY task, version"
            ).fetchall()
    return [dict(r) for r in rows]
