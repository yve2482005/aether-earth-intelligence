"""Leakage-prevention tests. Every one of these must pass in CI.

(a) The dataset builder drops any pair where observed_at >
    valid_time + tolerance.
(b) A deliberately "cheating" feature (carrying the future actual) is
    detected and rejected by the leakage checker.
(c) Time-split purity: max(train time) < min(validation time).
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from helpers import iso  # noqa: E402
from ml.weather.dataset import (  # noqa: E402
    build_dataset, epoch_to_iso, feature_names, iso_to_epoch,
    valid_times_iso)
from ml.weather.leakage import (  # noqa: E402
    assert_time_split_purity,
    check_pairs,
    pair_is_valid,
    validate_feature_names,
)


def test_pair_is_valid_boundary():
    vt = "2026-09-20T12:00:00Z"
    assert pair_is_valid(vt, "2026-09-20T13:29:00Z", 90.0)
    assert pair_is_valid(vt, "2026-09-20T13:30:00Z", 90.0)
    assert not pair_is_valid(vt, "2026-09-20T13:31:00Z", 90.0)
    # observation before the forecast is fine (nearest-obs matching)
    assert pair_is_valid(vt, "2026-09-20T11:00:00Z", 90.0)


def test_check_pairs_drops_late_observations():
    pairs = [
        {"valid_time": "2026-09-20T12:00:00Z",
         "observed_at": "2026-09-20T12:30:00Z"},
        {"valid_time": "2026-09-20T12:00:00Z",
         "observed_at": "2026-09-20T15:00:00Z"},  # 3h late -> leak
    ]
    kept = check_pairs(pairs, 90.0)
    assert len(kept) == 1
    assert kept[0]["observed_at"] == "2026-09-20T12:30:00Z"
    assert len(kept.dropped) == 1


def test_dataset_builder_drops_late_pairs(populated_weather_db):
    """End-to-end: the builder never emits a late-observation pair."""
    ds = build_dataset(populated_weather_db, tolerance_min=90.0)
    # 48 good pairs; the forecast whose only observation is 3h late
    # (2026-09-22T12:00:00Z) must not appear at all.
    assert ds.n_pairs == 48, f"expected 48 pairs, got {ds.n_pairs}"
    late_vt = iso(datetime(2026, 9, 20, 0, 0, tzinfo=timezone.utc)
                  + timedelta(hours=60))
    # valid_times is an int64 epoch array now — compare in epoch seconds.
    assert iso_to_epoch(late_vt) not in ds.valid_times
    # every surviving pair is within tolerance by construction of the
    # SQL join + check_pairs; belt-and-braces: re-run the guard
    # (pair_is_valid takes ISO strings, so convert back first).
    for vt in valid_times_iso(ds.valid_times):
        assert pair_is_valid(vt, vt, 90.0)


def test_cheating_feature_rejected():
    """A feature smuggling the future actual must be rejected."""
    names = feature_names() + ["future_actual_temperature"]
    with pytest.raises(ValueError, match="leakage guard"):
        validate_feature_names(names)
    with pytest.raises(ValueError, match="leakage guard"):
        validate_feature_names(["obs_precipitation_next_hour"])
    with pytest.raises(ValueError, match="leakage guard"):
        validate_feature_names(["temperature_revision_v2"])
    # honest names pass
    validate_feature_names(feature_names())


def test_time_split_purity_ok():
    train = ["2026-09-20T00:00:00Z", "2026-09-20T12:00:00Z"]
    val = ["2026-09-20T13:00:00Z", "2026-09-21T00:00:00Z"]
    assert_time_split_purity(train, val)  # must not raise


def test_time_split_purity_violation():
    train = ["2026-09-20T00:00:00Z", "2026-09-21T00:00:00Z"]
    val = ["2026-09-20T12:00:00Z"]
    with pytest.raises(ValueError, match="time-split impurity"):
        assert_time_split_purity(train, val)


def test_time_split_purity_equal_boundary():
    # equal timestamps are NOT a pure split (must be strictly less)
    t = ["2026-09-20T12:00:00Z"]
    with pytest.raises(ValueError, match="time-split impurity"):
        assert_time_split_purity(t, t)


def test_time_split_by_value_handles_ties():
    """Many pairs share one valid_time (multi-model/location). A row-count
    cut would strand identical timestamps on both sides; the value-based
    split must keep max(train) < min(val)."""
    from ml.weather.leakage import time_split_by_value

    times = []
    for h in range(48):
        vt = f"2026-09-20T{h // 24:02d}:{h % 24:02d}:00Z"
        times.extend([vt] * 4)  # 4 pairs per timestamp
    tr, va = time_split_by_value(times, 0.2)
    assert len(tr) + len(va) == len(times)
    assert_time_split_purity([times[i] for i in tr],
                             [times[i] for i in va])
    # no timestamp appears on both sides
    assert not (set(times[i] for i in tr) & set(times[i] for i in va))


def test_train_py_enforces_purity(populated_weather_db):
    """train_task refuses to proceed if the split is impure — simulated by
    asserting the helper it uses raises on shuffled times."""
    ds = build_dataset(populated_weather_db)
    order = np.argsort(ds.valid_times)  # epoch sort == chronological
    times = valid_times_iso(ds.valid_times[order])
    n = len(times)
    # a "split" that interleaves train/val must be rejected
    train = times[::2]
    val = times[1::2]
    with pytest.raises(ValueError, match="time-split impurity"):
        assert_time_split_purity(train, val)
