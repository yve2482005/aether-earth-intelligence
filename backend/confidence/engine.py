"""Confidence engine.

Scores a datum HIGH / MEDIUM / LOW from:
  - source freshness (data_age_min)
  - model agreement (spread across NWP models, for forecasts)
  - observation coverage (fraction of expected obs present)
  - forecast horizon (hours ahead)

Always returns a human-readable "why" list explaining the score.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


def assess(kind: str,
           data_age_min: Optional[float] = None,
           model_spread: Optional[float] = None,
           model_count: int = 1,
           obs_coverage: Optional[float] = None,
           horizon_hours: Optional[float] = None) -> Dict[str, Any]:
    why: List[str] = []
    penalties = 0

    if data_age_min is None:
        penalties += 1
        why.append("source age unknown — freshness cannot be verified")
    elif data_age_min > 180:
        penalties += 2
        why.append(f"source data is {data_age_min:.0f} min old (>3h) — stale")
    elif data_age_min > 60:
        penalties += 1
        why.append(f"source data is {data_age_min:.0f} min old (>1h) — aging")
    else:
        why.append(f"source data is fresh ({data_age_min:.0f} min old)")

    if kind == "FORECAST":
        if model_count >= 3:
            why.append(f"{model_count} models agree-checkable")
        elif model_count == 1:
            penalties += 1
            why.append("single model — no cross-model agreement check")
        if model_spread is not None:
            if model_spread > 3.0:
                penalties += 1
                why.append(f"models disagree (spread {model_spread:.1f}) — low agreement")
            else:
                why.append(f"models agree (spread {model_spread:.1f})")
        if horizon_hours is not None:
            if horizon_hours > 120:
                penalties += 1
                why.append(f"long horizon ({horizon_hours:.0f}h) — skill decays with lead time")
            else:
                why.append(f"horizon {horizon_hours:.0f}h within skillful range")

    if obs_coverage is not None:
        if obs_coverage < 0.5:
            penalties += 1
            why.append(f"observation coverage only {obs_coverage:.0%} — poorly anchored")
        else:
            why.append(f"observation coverage {obs_coverage:.0%}")

    level = "HIGH" if penalties == 0 else ("MEDIUM" if penalties <= 2 else "LOW")
    why.append(f"final confidence: {level} ({penalties} penalty point(s))")
    return {"level": level, "penalties": penalties, "why": why, "kind": kind}
