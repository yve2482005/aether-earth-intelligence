"""Data-leakage prevention for the AETHER weather ML pipeline.

Three independent guards (defense in depth):

1. PAIR-LEVEL: a forecast/observation pair is valid only if the observation
   was taken no later than valid_time + tolerance. Anything observed after
   that cannot have been known when the forecast was verified and must be
   dropped.
2. FEATURE-LEVEL: feature names are scanned for banned patterns
   (future/actual/observed values). A "cheating" feature that carries the
   answer is rejected before training starts.
3. TIME-SPLIT: training data must strictly precede validation data in time;
   walk-forward folds must be non-overlapping in their evaluation windows.

All guards raise on violation so CI fails loudly.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

import numpy as np

# Feature names may never reference values that would only be known after the
# forecast valid time (i.e. the observation itself or any revision of it).
# Matched as case-insensitive substrings: identifiers like
# "future_actual_temperature" or "obs_precip" use underscores, not word
# boundaries.
BANNED_FEATURE_PATTERNS = [
    "actual",
    "observed",
    "observation",
    "future",
    "revision",
    "obs_",
    "_obs",
    "target",
    "label",
]
_BANNED_RE = re.compile("|".join(BANNED_FEATURE_PATTERNS), re.IGNORECASE)


def parse_ts(ts: str) -> datetime:
    """Parse a UTC ISO-8601 timestamp (with or without trailing Z)."""
    s = ts.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


def pair_is_valid(valid_time: str, observed_at: str,
                  tolerance_min: float = 90.0) -> bool:
    """True iff the observation could legitimately verify this forecast."""
    return parse_ts(observed_at) <= parse_ts(valid_time) + timedelta(
        minutes=tolerance_min
    )


def check_pairs(pairs: list[dict], tolerance_min: float = 90.0) -> list[dict]:
    """Drop every pair where observed_at > valid_time + tolerance.

    Returns the surviving pairs. The dropped ones are returned via the
    ``dropped`` attribute on the returned list subclass for audit logging.
    """
    keep, dropped = [], []

    class Kept(list):
        pass

    out = Kept()
    for p in pairs:
        if pair_is_valid(p["valid_time"], p["observed_at"], tolerance_min):
            out.append(p)
        else:
            dropped.append(p)
    out.dropped = dropped  # type: ignore[attr-defined]
    return out


def validate_feature_names(feature_names: list[str]) -> None:
    """Reject any feature name that could carry future/observed information.

    Raises ValueError naming the offending features. Call before training.
    """
    bad = [n for n in feature_names if _BANNED_RE.search(n)]
    if bad:
        raise ValueError(
            "leakage guard: feature names reference future/observed values "
            f"and are rejected: {bad}"
        )


def assert_time_split_purity(train_times: list[str],
                             val_times: list[str]) -> None:
    """Max train time must be strictly below min validation time."""
    if not train_times or not val_times:
        raise ValueError("leakage guard: empty train or validation split")
    tmax = max(parse_ts(t) for t in train_times)
    vmin = min(parse_ts(t) for t in val_times)
    if not tmax < vmin:
        raise ValueError(
            "leakage guard: time-split impurity — max(train_time)="
            f"{tmax.isoformat()} is not < min(val_time)={vmin.isoformat()}"
        )


def assert_no_future_alerts(feature_cutoff: str,
                            alert_times: list[str]) -> None:
    """Alerts issued after the feature cutoff must never appear in features."""
    cutoff = parse_ts(feature_cutoff)
    late = [t for t in alert_times if parse_ts(t) > cutoff]
    if late:
        raise ValueError(
            "leakage guard: alerts issued after the feature cutoff would "
            f"leak into training: {late[:5]}"
        )


def time_split_by_value(valid_times: list[str],
                        val_frac: float = 0.2) -> tuple[np.ndarray, np.ndarray]:
    """Split indices into train/validation by time *value*, not row count.

    Many pairs share the same valid_time (multiple models/locations per
    hour); a row-count cut can land mid-tie and put identical timestamps on
    both sides. Instead the cutoff is a timestamp: train = time < cutoff,
    validation = time >= cutoff, so max(train) < min(validation) holds by
    construction. The validation share is approximate when ties are heavy.
    """
    times = [parse_ts(t) for t in valid_times]
    uniq = sorted(set(times))
    if len(uniq) < 3:
        raise ValueError(
            "leakage guard: too few distinct timestamps for a time split")
    k = max(1, int(len(uniq) * (1 - val_frac)))
    k = min(k, len(uniq) - 1)
    cutoff = uniq[k]
    tr = np.array([i for i, t in enumerate(times) if t < cutoff])
    va = np.array([i for i, t in enumerate(times) if t >= cutoff])
    if len(tr) < 10 or len(va) < 1:
        raise ValueError(
            f"leakage guard: degenerate time split "
            f"(train={len(tr)}, val={len(va)})")
    assert_time_split_purity(
        [valid_times[i] for i in tr], [valid_times[i] for i in va])
    return tr, va


def time_split_by_epoch(valid_times: np.ndarray,
                        val_frac: float = 0.2) -> tuple[np.ndarray, np.ndarray]:
    """Tie-safe value split over UTC epoch seconds (int64 array).

    Identical semantics to time_split_by_value — cutoff is a timestamp
    value, train = time < cutoff, validation = time >= cutoff, so
    max(train) < min(validation) holds by construction — but it works
    directly on the int64 epoch array stored by build_dataset, avoiding
    per-row datetime parsing on ~6M pairs (and avoiding rebuilding a
    6M-element ISO string list, which would defeat the array storage).
    """
    times = np.asarray(valid_times, dtype=np.int64)
    uniq = np.unique(times)
    if len(uniq) < 3:
        raise ValueError(
            "leakage guard: too few distinct timestamps for a time split")
    k = max(1, int(len(uniq) * (1 - val_frac)))
    k = min(k, len(uniq) - 1)
    cutoff = int(uniq[k])
    tr = np.nonzero(times < cutoff)[0]
    va = np.nonzero(times >= cutoff)[0]
    if len(tr) < 10 or len(va) < 1:
        raise ValueError(
            f"leakage guard: degenerate time split "
            f"(train={len(tr)}, val={len(va)})")
    if not int(times[tr].max()) < int(times[va].min()):
        raise ValueError(
            "leakage guard: time-split impurity — max(train_time) is not "
            "< min(val_time)")
    return tr, va
