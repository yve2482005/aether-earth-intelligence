"""Forecast replay / time machine (§§50-51).

Given a PAST date, return:
  - what the forecast said at the time (weather_forecasts valid within the
    date, latest run issued before each valid_time), vs
  - what was actually observed (weather_observations within the date),
  - the quake sequence (earthquake_events with origin_time in range),
  - tsunami message revisions (tsunami_messages in range, grouped per event).

STRICT SEPARATION: the replay window is always entirely in the past. Any
forecast row with valid_time in the future is excluded from the "what was
said" side for past dates; requesting a future date is rejected outright.
A dedicated test fails if an observation ever leaks into the forecast side.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional


class ReplayError(ValueError):
    """Raised when a replay request cannot be answered honestly."""


def _day_bounds(date_str: str) -> tuple[datetime, datetime]:
    try:
        day = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise ReplayError("date must be YYYY-MM-DD")
    return day, day + timedelta(days=1)


def replay(conn: sqlite3.Connection, date_str: str,
           location_id: Optional[int] = None) -> Dict[str, Any]:
    """Replay one past UTC date. All times are ISO-8601 UTC."""
    start, end = _day_bounds(date_str)
    now = datetime.now(timezone.utc)
    if start >= now.replace(hour=0, minute=0, second=0, microsecond=0):
        raise ReplayError(
            "replay covers past dates only — today and future dates are not replayable")
    start_s = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_s = end.strftime("%Y-%m-%dT%H:%M:%SZ")

    if location_id is None:
        row = conn.execute("SELECT id FROM locations ORDER BY id LIMIT 1").fetchone()
        if row is None:
            raise ReplayError("no locations in database")
        location_id = int(row["id"])
    loc = conn.execute("SELECT id, name FROM locations WHERE id = ?",
                       (location_id,)).fetchone()
    loc_name = dict(loc)["name"] if loc else str(location_id)

    # --- what the forecast said: forecasts valid within the day, from runs
    # issued BEFORE their valid time (never use hindsight) ---
    fc_rows = conn.execute(
        """SELECT f.model, f.forecast_run, f.forecast_hour, f.valid_time,
                  f.temperature, f.precip_probability, f.precipitation,
                  f.wind_speed, f.issued_at
           FROM weather_forecasts f
           WHERE f.location_id = ? AND f.valid_time >= ? AND f.valid_time < ?
             AND f.issued_at <= f.valid_time
           ORDER BY f.valid_time, f.model, f.issued_at DESC""",
        (location_id, start_s, end_s)).fetchall()
    # keep only the latest run per (model, valid_time) — what was known then
    seen: set = set()
    said: List[Dict[str, Any]] = []
    for r in fc_rows:
        r = dict(r)
        key = (r["model"], r["valid_time"])
        if key in seen:
            continue
        seen.add(key)
        # hard guard: no future-dated valid_time on the past side
        if r["valid_time"] >= now.strftime("%Y-%m-%dT%H:%M:%SZ"):
            continue
        said.append({k: r[k] for k in
                     ("model", "forecast_run", "forecast_hour", "valid_time",
                      "temperature", "precip_probability", "precipitation",
                      "wind_speed", "issued_at")})

    # --- what was observed: only observations inside the day ---
    obs_rows = conn.execute(
        """SELECT observed_at, station, temperature, humidity, pressure,
                  wind_speed, precipitation, source, quality
           FROM weather_observations
           WHERE location_id = ? AND observed_at >= ? AND observed_at < ?
           ORDER BY observed_at""",
        (location_id, start_s, end_s)).fetchall()
    observed = [dict(r) for r in obs_rows]

    # --- quake sequence in range ---
    quakes = [dict(r) for r in conn.execute(
        """SELECT canonical_event_id, origin_time, latitude, longitude, depth,
                  magnitude, magnitude_type, region, tsunami_flag
           FROM earthquake_events
           WHERE origin_time >= ? AND origin_time < ?
           ORDER BY origin_time""", (start_s, end_s)).fetchall()]

    # --- tsunami message revisions in range ---
    msgs = [dict(r) for r in conn.execute(
        """SELECT m.tsunami_event_id, m.message_type, m.issue_time,
                  m.received_at, m.source_url
           FROM tsunami_messages m
           WHERE m.issue_time >= ? AND m.issue_time < ?
           ORDER BY m.issue_time""", (start_s, end_s)).fetchall()]
    revisions: Dict[str, List[Dict[str, Any]]] = {}
    for m in msgs:
        revisions.setdefault(m["tsunami_event_id"], []).append(m)

    fc_min = min((s["valid_time"] for s in said), default=None)
    fc_max = max((s["valid_time"] for s in said), default=None)

    return {
        "date": date_str,
        "window": {"start": start_s, "end": end_s},
        "location_id": location_id,
        "location_name": loc_name,
        "past": {
            "forecast_said": said,          # valid within the day, issued before valid
            "observed": observed,           # observed within the day
            "forecast_span": {"first": fc_min, "last": fc_max},
            "forecast_count": len(said),
            "observation_count": len(observed),
        },
        "quakes": quakes,
        "tsunami_revisions": revisions,
        "separation": {
            "rule": ("Past side contains only forecasts valid within the "
                     "replayed day and observations taken within it. "
                     "No future valid_time ever appears here."),
            "future_valid_times_included": 0,
            "as_of": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        "coverage": {
            "has_forecasts": bool(said),
            "has_observations": bool(observed),
            "note": ("If a side is empty, that data was not collected then — "
                     "it is shown as missing, never reconstructed."),
        },
    }
