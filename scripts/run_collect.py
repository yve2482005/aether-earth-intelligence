#!/usr/bin/env python3
"""One-shot collector runner.

Collects for EVERY named location in the locations table (worldwide since
2026-10-16; before that, Yangon only):

- Primary location (Yangon, the configured default): full tier on every run —
  4 models, 7-day forecasts, observations, ocean. Behaviour unchanged.
- Every other location: 4 models, 7-day forecasts (3-day until 2026-10-17),
  observations, ocean when coastal — on the location's collection tier:
  Tier A (collect_every_hours=1, capitals + largest cities) once per hour,
  Tier B (collect_every_hours=6, everywhere else) once per 6 hours. The
  collector stores forecasts under an hour-truncated forecast_run and
  dedupes with INSERT OR IGNORE, so re-running inside the interval cannot
  add rows; the per-location interval guard therefore skips the HTTP
  fetches entirely (keeps the free Open-Meteo tier safe at the 5-minute
  cadence; all 4 models are fetched in one combined call per location
  since 2026-10-17).

Global feeds (USGS earthquakes, regional networks, tsunami sources and
observations, satellite, radar, aftershock outlook, alert rebuild) are
location-independent and run once per invocation, exactly as before.

Every collector is isolated: one failing never crashes the others.
"""
import json
import sys
from datetime import timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from backend.alerting import engine as alert_engine  # noqa: E402
from backend.common.config import DEFAULT_LAT, DEFAULT_LON, DEFAULT_LOCATION_NAME  # noqa: E402
from backend.common.http import make_session  # noqa: E402
from backend.common.logging_setup import setup_logging  # noqa: E402
from backend.common.timeutil import hour_floor, utc_iso  # noqa: E402
from backend.collectors.earthquake import aftershock, usgs  # noqa: E402
from backend.collectors.earthquake import regional_networks as eq_regional  # noqa: E402
from backend.collectors.tsunami import warning_sources as tsunami_src  # noqa: E402
from backend.collectors.tsunami import observations as tsunami_obs  # noqa: E402
from backend.collectors.weather import observations as wx_obs  # noqa: E402
from backend.collectors.weather import openmeteo  # noqa: E402
from backend.collectors.weather import radar, satellite  # noqa: E402
from backend.db import db as dbmod  # noqa: E402
from backend.engines import ocean as ocean_engine  # noqa: E402  (v2 workstream B)

log = setup_logging("aether.run_collect")

TABLES = ("weather_forecasts", "weather_observations", "earthquake_events",
          "earthquake_updates", "tsunami_events", "tsunami_messages",
          "earth_events", "alerts", "system_health")

# Worldwide tier horizon. Raised 3 -> 7 on 2026-10-17 for the 15 GB dataset
# scale-up (user order): 4 models x 7 days = 672 forecast rows per
# location-hour. API cost stays flat because all models are fetched in one
# combined Open-Meteo call per location (see openmeteo.collect).
WORLD_FORECAST_DAYS = 7


def counts(conn):
    return {t: dbmod.table_count(conn, t) for t in TABLES}


def get_or_create_location(conn, name, lat, lon):
    row = conn.execute(
        """SELECT * FROM locations WHERE ABS(latitude - ?) < 0.05 AND ABS(longitude - ?) < 0.05
           ORDER BY id LIMIT 1""", (lat, lon)).fetchone()
    if row:
        return dict(row)
    cur = conn.execute(
        """INSERT INTO locations (name, latitude, longitude, timezone, country, is_coastal, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (name, lat, lon, "Asia/Yangon", "Myanmar", 1, utc_iso()))
    conn.commit()
    return dict(conn.execute("SELECT * FROM locations WHERE id = ?",
                             (cur.lastrowid,)).fetchone())


def collectable_locations(conn):
    """All locations that carry a country (seeded, named places).

    The stray coordinate-named row with country NULL is excluded: it is not a
    real place entry and must not start collecting.
    """
    rows = conn.execute(
        "SELECT * FROM locations WHERE country IS NOT NULL ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def has_current_forecast_batch(conn, location_id, interval_hours=1):
    """True when this location already stored a forecast batch inside its
    collection-interval window (tier cadence, 2026-10-17 scale-up).

    Batches are stored under an hour-truncated forecast_run, so a location
    with interval N is due again once its newest stored run is N or more
    hours old: skip iff a run exists at or after (current hour - (N-1) h).
    interval_hours=1 reproduces the original current-hour check exactly.
    """
    try:
        interval = int(interval_hours or 1)
    except (TypeError, ValueError):
        interval = 1
    if interval < 1:
        interval = 1
    cutoff = utc_iso(hour_floor() - timedelta(hours=interval - 1))
    row = conn.execute(
        "SELECT 1 FROM weather_forecasts WHERE location_id = ? AND forecast_run >= ? LIMIT 1",
        (location_id, cutoff)).fetchone()
    return row is not None


def run_step(name, fn, conn=None):
    """Run one collector step in isolation.

    Transaction invariant (added 2026-10-18): a step must not leak an
    open transaction across its boundary. In sqlite3's legacy isolation
    mode a step that writes and then raises (or simply forgets to
    commit) leaves its connection holding the SQLite write lock while
    the worker moves on to slow network fetches — every other writer
    then starves at the busy_timeout, which is the exact signature of
    the recurring lock storms (the lock's holder is the collector
    itself, idle, with all workers parked in the busy handler). On
    success a dangling transaction is committed (the step's work is
    complete); on failure it is rolled back (partial writes of a
    failed step must not land). Both events are logged with the step
    name so the leaking step is identifiable from the log alone.
    """
    try:
        result = fn()
    except Exception as e:  # noqa: BLE001 - isolation per collector
        if conn is not None:
            try:
                if conn.in_transaction:
                    conn.rollback()
                    log.warning("step %s failed and left an open "
                                "transaction; rolled back", name)
            except Exception:  # noqa: BLE001 - never mask the real error
                log.exception("step %s: rollback after failure failed", name)
        log.exception("collector %s failed", name)
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    if conn is not None:
        try:
            if conn.in_transaction:
                conn.commit()
                log.warning("step %s left an open transaction; "
                            "committed at step boundary", name)
        except Exception:  # noqa: BLE001 - surface via the step result
            log.exception("step %s: boundary commit failed", name)
    return {"ok": True, "result": result}


def collect_location(conn, loc, session, primary):
    """Weather + observations (+ ocean when coastal) for one location."""
    lid, lat, lon = loc["id"], loc["latitude"], loc["longitude"]
    out = {}
    if primary:
        out["weather_forecasts"] = run_step(
            f"openmeteo:{loc['name']}",
            lambda: openmeteo.collect(conn, lid, lat, lon, session=session), conn=conn)
        out["weather_observations"] = run_step(
            f"wx_observations:{loc['name']}",
            lambda: wx_obs.collect(conn, lid, lat, lon, session=session), conn=conn)
        out["ocean"] = run_step(
            f"openmeteo-marine:{loc['name']}",
            lambda: ocean_engine.collect(conn, lid, lat, lon, session=session), conn=conn)
        return out
    interval = loc.get("collect_every_hours") or 1
    if has_current_forecast_batch(conn, lid, interval):
        return {"skipped": f"forecast batch already stored within the "
                           f"{interval}h collection interval"}
    out["weather_forecasts"] = run_step(
        f"openmeteo:{loc['name']}",
        lambda: openmeteo.collect(conn, lid, lat, lon, session=session,
                                  forecast_days=WORLD_FORECAST_DAYS), conn=conn)
    out["weather_observations"] = run_step(
        f"wx_observations:{loc['name']}",
        lambda: wx_obs.collect(conn, lid, lat, lon, session=session), conn=conn)
    if loc.get("is_coastal"):
        out["ocean"] = run_step(
            f"openmeteo-marine:{loc['name']}",
            lambda: ocean_engine.collect(conn, lid, lat, lon, session=session), conn=conn)
    return out


def main() -> None:
    dbmod.init_db()
    conn = dbmod.connect()
    before = counts(conn)
    primary = get_or_create_location(conn, DEFAULT_LOCATION_NAME, DEFAULT_LAT, DEFAULT_LON)
    locations = collectable_locations(conn)
    if not any(l["id"] == primary["id"] for l in locations):
        locations.insert(0, primary)
    session = make_session()

    summary = {}
    loc_results = {}
    for loc in locations:
        loc_results[loc["name"]] = collect_location(
            conn, loc, session, primary=(loc["id"] == primary["id"]))
    summary["locations"] = loc_results
    # Backwards-compatible top-level keys reflect the primary location.
    for key in ("weather_forecasts", "weather_observations", "ocean"):
        if key in loc_results.get(primary["name"], {}):
            summary[key] = loc_results[primary["name"]][key]

    summary["satellite"] = run_step("satellite", lambda: satellite.collect(conn), conn=conn)
    summary["radar"] = run_step("radar", lambda: radar.collect(conn), conn=conn)
    summary["usgs"] = run_step("usgs", lambda: usgs.collect(conn, session=session), conn=conn)
    summary["regional_networks"] = run_step("regional", lambda: eq_regional.collect(conn), conn=conn)
    summary["tsunami_warnings"] = run_step("tsunami",
        lambda: tsunami_src.collect(conn, session=session), conn=conn)
    summary["tsunami_observations"] = run_step("tsunami_obs",
        lambda: tsunami_obs.collect(conn), conn=conn)
    # Aftershock outlook: check the largest known event only (1 ComCat detail fetch).
    summary["aftershock"] = run_step("aftershock",
        lambda: aftershock.get_aftershock_forecast(conn, session=session), conn=conn)
    summary["alerts"] = run_step("alert_engine", lambda: alert_engine.build_alerts(conn), conn=conn)

    after = counts(conn)
    conn.close()

    collected = [n for n, r in loc_results.items() if "skipped" not in r]
    skipped = [n for n, r in loc_results.items() if "skipped" in r]
    print(f"locations: {len(locations)} total, {len(collected)} collected, "
          f"{len(skipped)} skipped (hour-guard)")
    print(f"primary location: {primary['name']} id={primary['id']} "
          f"({primary['latitude']}, {primary['longitude']})")
    print("\nrows per table (before -> after):")
    for t in TABLES:
        print(f"  {t:22s} {before[t]:6d} -> {after[t]:6d} (+{after[t]-before[t]})")
    print("\ncollector summary:")
    for name, s in summary.items():
        if name == "locations":
            failed = [n for n, r in s.items()
                      if any(isinstance(v, dict) and not v.get("ok", True)
                             for v in r.values())]
            print(f"  {'locations':20s} {len(collected)} collected, "
                  f"{len(skipped)} skipped, {len(failed)} with failures"
                  + (f": {failed}" if failed else ""))
            continue
        if s.get("ok"):
            print(f"  {name:20s} OK")
        else:
            print(f"  {name:20s} FAILED: {s.get('error')}")
    print("\nfull summary JSON:")
    print(json.dumps(summary, indent=1, default=str)[:4000])


if __name__ == "__main__":
    main()
