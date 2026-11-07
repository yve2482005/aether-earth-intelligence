"""Fan-out window tests (2026-11-06).

Real incident: the old window ordering (primary, hourly tier,
least-recently-submitted) filled the whole 80-slot window with Tier A
every run (253 Tier A > 80), so Tier B was NEVER submitted — its
forecasts went stale on 2026-10-18 and it collected zero observations.
The rotation file proved it: 253 Tier A ids, 0 Tier B ids.

The fix splits the window: a guaranteed Tier B share (default 20 of 80)
so every tier cycles through.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.collectors.advanced import _select_fanout_window  # noqa: E402


def _locs(n_a=253, n_b=753):
    locs = [{"id": 1, "collect_every_hours": 1}]
    locs += [{"id": i, "collect_every_hours": 1} for i in range(2, n_a + 1)]
    locs += [{"id": 1000 + i, "collect_every_hours": 6}
             for i in range(1, n_b + 1)]
    return locs


def test_window_mixes_both_tiers():
    locs = _locs()
    window, _ = _select_fanout_window(locs, 1, {}, 80)
    assert len(window) == 80
    n_b = sum(1 for l in window if l["collect_every_hours"] == 6)
    n_a = sum(1 for l in window if l["collect_every_hours"] == 1)
    assert n_b == 20
    assert n_a == 60
    assert window[0]["id"] == 1  # primary first


def test_repeated_windows_cycle_all_tier_b():
    locs = _locs()
    rotation = {}
    seen_b = set()
    for _ in range(40):
        window, rotation = _select_fanout_window(locs, 1, rotation, 80)
        seen_b |= {l["id"] for l in window
                   if l["collect_every_hours"] == 6}
    assert len(seen_b) == 753


def test_repeated_windows_cycle_all_tier_a():
    locs = _locs()
    rotation = {}
    seen_a = set()
    for _ in range(5):
        window, rotation = _select_fanout_window(locs, 1, rotation, 80)
        seen_a |= {l["id"] for l in window
                   if l["collect_every_hours"] == 1}
    assert len(seen_a) == 253


def test_short_tier_yields_slots_to_other():
    only_a = [{"id": i, "collect_every_hours": 1} for i in range(1, 51)]
    window, _ = _select_fanout_window(only_a, 1, {}, 80)
    assert len(window) == 50
    only_b = [{"id": i, "collect_every_hours": 6} for i in range(1, 31)]
    window, _ = _select_fanout_window(only_b, 999, {}, 80)
    assert len(window) == 30
