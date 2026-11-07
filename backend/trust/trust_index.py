"""Trust Index (§46).

A TRANSPARENCY INDICATOR — never a correctness claim. Combines six
honest, auditable components:

  source_quality     — mean DATA QUALITY SCORE of the datasets in use
  freshness          — age of the latest data used
  model_agreement    — spread across models for the same valid time
                       (low spread -> higher agreement)
  historical_skill   — 1 - mae(best verified model) / mae(climatology baseline)
  observation_agreement — fraction of recent forecast/obs pairs within tolerance
  uncertainty        — model spread expressed as a penalty

Every component is returned with its method and raw inputs, so the number
can be inspected rather than believed. Always labeled:
"transparency indicator, not a correctness guarantee".
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from backend.common.timeutil import parse_iso
from backend.trust.quality import all_datasets_quality, _freshness_score
from backend.trust.verification import baseline_scores

LABEL = "transparency indicator, not a correctness guarantee"


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _model_agreement(conn: sqlite3.Connection, location_id: int) -> tuple[float, str]:
    """1 - normalized spread of latest forecasts across models."""
    rows = conn.execute(
        """SELECT model, temperature FROM weather_forecasts
           WHERE location_id = ? AND forecast_hour = 24
             AND valid_time = (SELECT MAX(valid_time) FROM weather_forecasts
                               WHERE location_id = ? AND forecast_hour = 24)
             AND temperature IS NOT NULL""",
        (location_id, location_id)).fetchall()
    temps = [r["temperature"] for r in rows]
    if len(temps) < 2:
        return 0.5, (f"only {len(temps)} model(s) for latest +24h forecast — "
                     "agreement unknown, scored neutral")
    spread = max(temps) - min(temps)
    score = _clamp01(1.0 - spread / 10.0)  # 10C spread -> 0
    return score, (f"{len(temps)} models, +24h temperature spread "
                   f"{round(spread, 2)}C")


def _historical_skill(conn: sqlite3.Connection, location_id: int) -> tuple[float, str]:
    """1 - mae(model) / mae(climatology), from stored scores + computed baseline."""
    row = conn.execute(
        "SELECT model, mae FROM model_scores WHERE location_id = ? "
        "AND variable = 'temperature' AND horizon = 24 "
        "AND mae IS NOT NULL AND mae > 0 ORDER BY period_end DESC LIMIT 1",
        (location_id,)).fetchone()
    if row is None:
        return 0.5, "no verified model scores for temperature +24h — scored neutral"
    try:
        base = baseline_scores(conn, location_id, "temperature", 24)
        clim = base["baselines"].get("climatology", {})
        clim_mae = clim.get("mae")
    except Exception as e:  # noqa: BLE001 - baseline failure must not break the index
        return 0.5, f"climatology baseline unavailable ({e}) — scored neutral"
    if not clim_mae or clim_mae <= 0:
        return 0.5, "climatology baseline has no MAE — scored neutral"
    skill = _clamp01(1.0 - float(row["mae"]) / float(clim_mae))
    return skill, (f"verified {row['model']} MAE {round(float(row['mae']), 2)} vs "
                   f"climatology MAE {round(float(clim_mae), 2)}")


def _observation_agreement(conn: sqlite3.Connection, location_id: int,
                           tol_c: float = 2.0) -> tuple[float, str]:
    """Fraction of recent +24h temperature pairs within tolerance."""
    rows = conn.execute(
        """SELECT f.temperature AS fc, o.temperature AS ob
           FROM weather_forecasts f JOIN weather_observations o
             ON o.location_id = f.location_id AND o.observed_at = f.valid_time
           WHERE f.location_id = ? AND f.forecast_hour = 24
             AND f.temperature IS NOT NULL AND o.temperature IS NOT NULL
           ORDER BY f.valid_time DESC LIMIT 200""", (location_id,)).fetchall()
    pairs = [(r["fc"], r["ob"]) for r in rows]
    if not pairs:
        return 0.5, "no recent forecast/observation pairs — scored neutral"
    agree = sum(1 for f, o in pairs if abs(f - o) <= tol_c) / len(pairs)
    return agree, f"{len(pairs)} recent pairs, {round(agree * 100, 1)}% within {tol_c}C"


def _uncertainty(conn: sqlite3.Connection, location_id: int) -> tuple[float, str]:
    """Penalty from current model spread (same inputs as agreement)."""
    score, note = _model_agreement(conn, location_id)
    return score, "derived from model agreement: " + note


def compute_trust_index(conn: sqlite3.Connection,
                        location_id: Optional[int] = None) -> Dict[str, Any]:
    """Compute the Trust Index for a location (default: first location)."""
    if location_id is None:
        row = conn.execute("SELECT id FROM locations ORDER BY id LIMIT 1").fetchone()
        if row is None:
            raise ValueError("no locations in database")
        location_id = int(row["id"])

    loc = conn.execute("SELECT id, name FROM locations WHERE id = ?",
                       (location_id,)).fetchone()
    loc_name = dict(loc)["name"] if loc else str(location_id)

    qualities = all_datasets_quality(conn)
    q_scores = [q["score"] for q in qualities if q.get("score") is not None]
    source_quality = sum(q_scores) / len(q_scores) / 100.0 if q_scores else 0.5
    q_note = (f"mean data-quality score {round(source_quality * 100, 1)} "
              f"over {len(q_scores)} datasets")

    latest = conn.execute(
        """SELECT MAX(collected_at) AS t FROM (
             SELECT collected_at FROM weather_forecasts
             UNION ALL SELECT collected_at FROM weather_observations)"""
    ).fetchone()["t"]
    age_min = None
    dt = parse_iso(latest) if latest else None
    if dt:
        age_min = (datetime.now(timezone.utc) - dt).total_seconds() / 60.0
    freshness = _freshness_score(age_min)
    f_note = (f"latest weather data {round(age_min, 1)} min old"
              if age_min is not None else "no weather timestamps — scored stale")

    agreement, a_note = _model_agreement(conn, location_id)
    skill, s_note = _historical_skill(conn, location_id)
    obs_agree, o_note = _observation_agreement(conn, location_id)
    uncertainty, u_note = _uncertainty(conn, location_id)

    components = {
        "source_quality": round(source_quality, 3),
        "freshness": round(freshness, 3),
        "model_agreement": round(agreement, 3),
        "historical_skill": round(skill, 3),
        "observation_agreement": round(obs_agree, 3),
        "uncertainty": round(uncertainty, 3),
    }
    weights = {"source_quality": 0.20, "freshness": 0.20, "model_agreement": 0.15,
               "historical_skill": 0.15, "observation_agreement": 0.15,
               "uncertainty": 0.15}
    index = round(sum(components[k] * weights[k] for k in components) * 100, 1)
    band = ("high" if index >= 75 else "moderate" if index >= 50
            else "low" if index >= 25 else "very low")

    return {
        "location_id": location_id,
        "location_name": loc_name,
        "trust_index": index,
        "band": band,
        "label": LABEL,
        "components": components,
        "component_notes": {
            "source_quality": q_note, "freshness": f_note,
            "model_agreement": a_note, "historical_skill": s_note,
            "observation_agreement": o_note, "uncertainty": u_note,
        },
        "data_age_minutes": round(age_min, 1) if age_min is not None else None,
        "computed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "disclaimer": ("The Trust Index measures how transparent and "
                       "well-supported the data is — it is NOT a correctness "
                       "guarantee and NOT a probability."),
    }
