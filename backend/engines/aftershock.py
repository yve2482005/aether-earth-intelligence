"""Aftershock statistics (spec §8) — statistical only, NEVER a prediction.

Uses Omori's law for the decay of aftershock *rates* and the Gutenberg-Richter
relation for *relative* magnitude distribution, with generic (uncalibrated)
parameters. Output is explicitly labeled "statistical aftershock probability,
not a prediction": it describes how aftershock sequences *typically* behave,
not what *will* happen for any specific earthquake. No specific event is ever
forecast.
"""
from __future__ import annotations

import math
import sqlite3
from typing import Any, Dict, Optional

from backend.common.timeutil import age_minutes, utc_iso

DISCLAIMER = ("Statistical aftershock probability, not a prediction. "
              "Generic Omori/Gutenberg-Richter parameters, not calibrated to this "
              "sequence. Describes typical sequence behavior — never a forecast "
              "of any specific earthquake.")

# Generic Omori parameters (rate per day at t->0 for a reference sequence).
_OMORI_K = 10.0
_OMORI_C_DAYS = 0.05
_OMORI_P = 1.0
_GR_B = 1.0  # Gutenberg-Richter b-value (generic)


def _omori_rate_per_day(t_days: float) -> float:
    t_days = max(t_days, 1e-6)
    return _OMORI_K / ((_OMORI_C_DAYS + t_days) ** _OMORI_P)


def statistics_for(conn: Optional[sqlite3.Connection], magnitude: Optional[float],
                   origin_time: Optional[str],
                   mainshock_id: Optional[str] = None) -> Dict[str, Any]:
    """Statistical aftershock picture for a mainshock. conn unused (kept for API symmetry)."""
    if magnitude is None or origin_time is None:
        return {"status": "unavailable", "kind": "PROBABILITY",
                "message": "Mainshock magnitude/origin time missing — no statistics possible.",
                "disclaimer": DISCLAIMER}
    age_min = age_minutes(origin_time)
    age_days = (age_min or 0.0) / 1440.0

    # Expected largest aftershock ~ M_main - 1.2 (Bath's law, typical).
    expected_largest = round(magnitude - 1.2, 1)
    # Relative rate now vs 1 hour after the mainshock.
    rate_now = _omori_rate_per_day(age_days)
    rate_1h = _omori_rate_per_day(1.0 / 24.0)
    relative = rate_now / rate_1h if rate_1h else 0.0
    # Probability of at least one aftershock >= M in the next 24h (Poisson, generic rate
    # scaled by Gutenberg-Richter relative to the reference sequence at M5).
    out: Dict[str, Any] = {
        "status": "ok",
        "kind": "PROBABILITY",
        "mainshock_id": mainshock_id,
        "mainshock_magnitude": magnitude,
        "sequence_age_days": round(age_days, 2),
        "expected_largest_aftershock_typical": expected_largest,
        "rate_now_relative_to_1h": round(relative, 3),
        "windows": [],
        "disclaimer": DISCLAIMER,
    }
    for label, days in (("next_24h", 1.0), ("next_7d", 7.0)):
        # Expected count of M>=5 aftershocks in window, generic scaling.
        lam = _omori_rate_per_day(age_days) * days * (10 ** (-_GR_B * (5.0 - 5.0)))
        lam *= 10 ** (0.5 * (magnitude - 6.0))  # larger mainshocks -> richer sequences
        p_at_least_one = 1.0 - math.exp(-max(lam, 0.0))
        out["windows"].append({
            "window": label,
            "probability_at_least_one_M5plus": round(min(p_at_least_one, 0.99), 3),
            "kind": "PROBABILITY",
            "note": "Generic statistical model — not a prediction for this sequence.",
        })
    out["assessed_at"] = utc_iso()
    return out
