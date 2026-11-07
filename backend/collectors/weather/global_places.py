"""Global places registry + bulk raw current-conditions collector.

User order 2026-10-17: collect raw data for 200,000 cities.

Two pieces:

1. REGISTRY — the free GeoNames `cities500` dump (populated places with
   population >= 500, CC-BY, no key) loaded into a dedicated
   `global_places` table. This is a *place index*, separate from the
   curated `locations` table that the deep tiered collectors own.

2. BULK COLLECTOR — sweeps every registered place with the Open-Meteo
   forecast API's multi-coordinate form (comma-separated lat/lon lists in
   ONE call; the response is a JSON array, one object per coordinate, in
   request order — verified live 2026-10-17). Each place yields one raw
   current-conditions snapshot per sweep.

Storage honesty: snapshots go into a dedicated `global_place_weather`
table, NOT `weather_forecasts`. `weather_forecasts.location_id` is a
NOT NULL FK to the curated `locations` table and its rows are per-model
forecasts keyed by (forecast_run, forecast_hour); a best-model current
snapshot for a GeoNames place is neither, so forcing it in there would
require fabricating 200k `locations` rows and a fake model/run identity.
A raw snapshot table keyed by geoname_id says exactly what the data is.

Sweeps are checkpointed in `global_collect_state` (completed batch
indices as JSON): a killed or rate-limited sweep resumes where it
stopped. On HTTP 429 the sweep backs off and STOPS — it never hammers.

Validation: the advanced collector's ingest ranges are reused verbatim
(temperature -90..60, humidity 0..100, pressure 800..1100, wind >= 0,
precipitation >= 0). A reading with an out-of-range field is skipped
and counted, never stored. NULL fields pass the gate (absence is not a
range violation), matching the project-wide gate semantics.
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from backend.collectors.advanced import VALIDATION_RANGES, value_in_range
from backend.common.http import fetch_json
from backend.common.logging_setup import setup_logging
from backend.common.timeutil import utc_iso

log = setup_logging("aether.collectors.weather.global_places")

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
CURRENT_VARS = ",".join([
    "temperature_2m", "relative_humidity_2m", "pressure_msl",
    "wind_speed_10m", "precipitation", "weather_code",
])
DEFAULT_BATCH_SIZE = 250
# Batch size verified live 2026-10-17: 250 coordinates/call succeeds
# (2.9 s, all entries parsed); 500 fails with HTTP 414 (URL too long).
# A full sweep at batch=250 over ~236k places is ~946 calls — under a
# tenth of the shared ~10k/day free-tier guidance. MAX_BATCHES_PER_RUN
# keeps a single invocation from ever blowing that budget even if
# resumed repeatedly.
MAX_BATCHES_PER_RUN = 5000
STOP_AFTER_CONSECUTIVE_FAILURES = 3

DDL = """
CREATE TABLE IF NOT EXISTS global_places (
    geoname_id   INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    ascii_name   TEXT,
    country_code TEXT,
    country_name TEXT,
    latitude     REAL NOT NULL,
    longitude    REAL NOT NULL,
    population   INTEGER,
    timezone     TEXT,
    elevation    REAL
);
CREATE INDEX IF NOT EXISTS idx_global_places_name ON global_places(name);
CREATE INDEX IF NOT EXISTS idx_global_places_ascii ON global_places(ascii_name);
CREATE INDEX IF NOT EXISTS idx_global_places_country ON global_places(country_code);

CREATE TABLE IF NOT EXISTS global_place_weather (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    geoname_id    INTEGER NOT NULL REFERENCES global_places(geoname_id),
    observed_at   TEXT NOT NULL,
    temperature   REAL,
    humidity      REAL,
    pressure      REAL,
    wind_speed    REAL,
    precipitation REAL,
    weather_code  INTEGER,
    fetched_at    TEXT NOT NULL,
    UNIQUE (geoname_id, observed_at)
);
CREATE INDEX IF NOT EXISTS idx_gpw_place ON global_place_weather(geoname_id);
CREATE INDEX IF NOT EXISTS idx_gpw_observed ON global_place_weather(observed_at);

CREATE TABLE IF NOT EXISTS global_collect_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)
    conn.commit()


# ---------------------------------------------------------------------------
# GeoNames parsing (pure — unit-testable without files or network)
# ---------------------------------------------------------------------------

def parse_country_info(lines: Iterable[str]) -> Dict[str, str]:
    """countryInfo.txt -> {ISO code: country name}. Comment/blank lines skip."""
    out: Dict[str, str] = {}
    for line in lines:
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.rstrip("\n").split("\t")
        if len(parts) >= 5 and parts[0]:
            out[parts[0]] = parts[4]
    return out


def parse_cities500(lines: Iterable[str],
                    country_names: Dict[str, str]) -> List[Tuple]:
    """Parse cities500 TSV lines into global_places row tuples.

    GeoNames columns: 0 geonameid, 1 name, 2 asciiname, 4 latitude,
    5 longitude, 8 country code, 14 population, 15 elevation (often
    empty), 16 dem (fallback elevation), 17 timezone. Malformed lines
    are skipped, never guessed at.
    """
    rows: List[Tuple] = []
    for line in lines:
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 19:
            continue
        try:
            geoname_id = int(parts[0])
            lat = float(parts[4])
            lon = float(parts[5])
        except ValueError:
            continue
        pop = None
        if parts[14]:
            try:
                pop = int(parts[14])
            except ValueError:
                pop = None
        elev = None
        for raw in (parts[15], parts[16]):
            if raw:
                try:
                    elev = float(raw)
                    break
                except ValueError:
                    continue
        cc = parts[8] or None
        rows.append((
            geoname_id, parts[1], parts[2] or None, cc,
            country_names.get(cc) if cc else None,
            lat, lon, pop, parts[17] or None, elev,
        ))
    return rows


def load_registry(conn: sqlite3.Connection, cities_lines: Iterable[str],
                  country_lines: Iterable[str],
                  chunk: int = 5000) -> Dict[str, Any]:
    """Idempotent registry load: parse + INSERT OR REPLACE in chunks."""
    ensure_tables(conn)
    countries = parse_country_info(country_lines)
    rows = parse_cities500(cities_lines, countries)
    for i in range(0, len(rows), chunk):
        conn.executemany(
            """INSERT OR REPLACE INTO global_places
               (geoname_id, name, ascii_name, country_code, country_name,
                latitude, longitude, population, timezone, elevation)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            rows[i:i + chunk])
        conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM global_places").fetchone()[0]
    ncountries = conn.execute(
        "SELECT COUNT(DISTINCT country_code) FROM global_places").fetchone()[0]
    return {"parsed": len(rows), "places": total, "countries": ncountries}


# ---------------------------------------------------------------------------
# Bulk response parsing (pure — unit-testable on a recorded payload)
# ---------------------------------------------------------------------------

def split_batches(items: Sequence, size: int) -> List[List]:
    if size <= 0:
        raise ValueError("batch size must be positive")
    return [list(items[i:i + size]) for i in range(0, len(items), size)]


def build_params(batch: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Multi-coordinate forecast-API params for one batch of places."""
    return {
        "latitude": ",".join(str(p["latitude"]) for p in batch),
        "longitude": ",".join(str(p["longitude"]) for p in batch),
        "current": CURRENT_VARS,
        "timezone": "UTC",
        "wind_speed_unit": "ms",
        "precipitation_unit": "mm",
        "forecast_days": 1,
    }


def _to_iso_z(ts: str) -> str:
    """'2026-10-17T15:30' (UTC, from the API) -> '2026-10-17T15:30:00Z'."""
    ts = ts.strip()
    if ts.endswith("Z"):
        return ts
    if len(ts) == 16:
        ts += ":00"
    return ts + "Z"


def parse_bulk_response(data: Any, batch: Sequence[Dict[str, Any]]
                        ) -> List[Dict[str, Any]]:
    """Split a multi-coordinate response into per-place readings.

    The API returns a JSON array in request order (a bare object when a
    single coordinate was requested). Entries are matched by order and
    sanity-checked against the requested coordinates (grid snapping
    moves them slightly; >1 degree off means the pairing is wrong and
    the entry is dropped rather than misattributed).
    """
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return []
    out: List[Dict[str, Any]] = []
    for place, entry in zip(batch, data):
        if not isinstance(entry, dict):
            continue
        try:
            if abs(float(entry["latitude"]) - place["latitude"]) > 1.0 or \
               abs(float(entry["longitude"]) - place["longitude"]) > 1.0:
                continue
        except (KeyError, TypeError, ValueError):
            continue
        cur = entry.get("current") or {}
        if not cur.get("time"):
            continue
        out.append({
            "geoname_id": place["geoname_id"],
            "observed_at": _to_iso_z(str(cur["time"])),
            "temperature": cur.get("temperature_2m"),
            "humidity": cur.get("relative_humidity_2m"),
            "pressure": cur.get("pressure_msl"),
            "wind_speed": cur.get("wind_speed_10m"),
            "precipitation": cur.get("precipitation"),
            "weather_code": cur.get("weather_code"),
        })
    return out


# Reading field -> gate field name in VALIDATION_RANGES.
_GATE_FIELDS = ("temperature", "humidity", "pressure", "wind_speed",
                "precipitation")


def reading_in_range(reading: Dict[str, Any]) -> bool:
    """True when every gated field of the reading passes the ingest gate."""
    return all(f in VALIDATION_RANGES and value_in_range(f, reading.get(f))
               for f in _GATE_FIELDS)


# ---------------------------------------------------------------------------
# Sweep state (checkpoint / resume)
# ---------------------------------------------------------------------------

def _state_get(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM global_collect_state WHERE key=?",
                       (key,)).fetchone()
    return row[0] if row else None


def _collector_active() -> bool:
    """True while an advanced collector holds its single-instance flock."""
    import fcntl
    from pathlib import Path

    path = (Path(__file__).resolve().parents[3] / "data"
            / ".collect_advanced.lock")
    try:
        f = open(path, "a+")
    except OSError:
        return False
    try:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(f, fcntl.LOCK_UN)
        return False
    finally:
        f.close()


def _wait_for_collector_window(conn, max_wait_s: float = 600.0) -> bool:
    """Yield write priority to the 5-minute collector before a batch.

    The 200k-city sweep is background filler; earthquake/weather
    collection is the priority traffic. On this VM's slow disk two
    cold writers degrade into mutual lock storms (watchdog caught the
    sweep blocking the collector for ~17 min on 2026-10-18). Returns
    True when a free window was seen (or this is not the production
    DB — test sweeps never wait); False when the collector stayed
    busy for the whole wait, in which case the caller DEFERS the
    segment (checkpoint intact, next pass retries) instead of ramming
    into the storm and failing — the earlier proceed-anyway behaviour
    turned every pass's bulk stage into a lock failure.
    """
    try:
        files = [r[2] for r in conn.execute("PRAGMA database_list")]
    except Exception:  # noqa: BLE001
        files = []
    if not any(str(f).endswith("data/aether.db") for f in files):
        return True
    waited = 0.0
    while _collector_active() and waited < max_wait_s:
        time.sleep(15)
        waited += 15
    if waited:
        print(f"bulk yielded {waited:.0f}s to collector", flush=True)
    return not _collector_active()


def _write_with_retry(fn, attempts: int = 6, delay_s: float = 2.0):
    """Run a short write callable, retrying transient SQLite lock errors.

    The production DB is shared with the 5-minute collectors: in WAL
    mode a write can fail immediately with SQLITE_BUSY_SNAPSHOT (which
    busy_timeout does not retry) when another writer commits while our
    read snapshot is open. Observed live 2026-10-17 — a sweep died on
    'database is locked' next to an hourly collection batch. Short
    writes + bounded retries are the fix; anything else re-raises.
    """
    for attempt in range(attempts):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) or attempt == attempts - 1:
                raise
            time.sleep(delay_s)


def _state_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    def _do() -> None:
        conn.execute(
            """INSERT INTO global_collect_state (key, value, updated_at)
               VALUES (?,?,?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                                              updated_at=excluded.updated_at""",
            (key, value, utc_iso()))
        conn.commit()

    _write_with_retry(_do)


def load_sweep_state(conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
    raw = _state_get(conn, "sweep")
    return json.loads(raw) if raw else None


def _save_state_best_effort(conn: sqlite3.Connection,
                            state: Dict[str, Any]) -> None:
    """save_sweep_state that cannot kill the sweep (2026-10-18).

    Batch inserts are INSERT OR IGNORE (idempotent) and the persisted
    checkpoint only advances on success, so a state save that loses a
    lock race is cosmetic: retry once after a short pause, then log
    and continue in-memory. Before this, an unguarded save inside the
    failure branch threw OperationalError out of run_sweep and turned
    the whole bulk stage into an exit-1 failure even though the sweep
    itself was checkpoint-safe.
    """
    try:
        save_sweep_state(conn, state)
    except sqlite3.Error:
        time.sleep(20)
        try:
            save_sweep_state(conn, state)
        except sqlite3.Error as exc:
            print(f"bulk state save deferred (lock): {exc}", flush=True)


def save_sweep_state(conn: sqlite3.Connection, state: Dict[str, Any]) -> None:
    state["updated_at"] = utc_iso()
    _state_set(conn, "sweep", json.dumps(state))


def pending_batches(state: Dict[str, Any]) -> List[int]:
    """Batch indices not yet completed — the resume work list."""
    done = set(state.get("completed", []))
    return [i for i in range(state["total_batches"]) if i not in done]


# ---------------------------------------------------------------------------
# Sweep runner
# ---------------------------------------------------------------------------

def run_sweep(conn: sqlite3.Connection, session,
              batch_size: int = DEFAULT_BATCH_SIZE,
              max_batches: Optional[int] = None,
              fresh: bool = False,
              sleep_s: float = 0.0) -> Dict[str, Any]:
    """Run (or resume) a bulk sweep over global_places.

    Returns a summary dict. Stops gracefully on HTTP 429 (checkpoint
    saved, status 'stopped_429') and after repeated batch failures.
    Individual failed batches stay pending and are retried on resume.
    """
    ensure_tables(conn)
    places = conn.execute(
        "SELECT geoname_id, latitude, longitude FROM global_places "
        "ORDER BY geoname_id").fetchall()
    places = [{"geoname_id": r[0], "latitude": r[1], "longitude": r[2]}
              for r in places]
    conn.commit()  # release the read snapshot before any writes (WAL)
    batches = split_batches(places, batch_size)
    total_batches = len(batches)

    state = None if fresh else load_sweep_state(conn)
    if state and (state.get("batch_size") != batch_size
                  or state.get("total_places") != len(places)
                  or state.get("status") == "complete"):
        state = None  # registry or batching changed -> new sweep
    if state is None:
        state = {
            "sweep_id": utc_iso(), "started_at": utc_iso(),
            "batch_size": batch_size, "total_places": len(places),
            "total_batches": total_batches, "completed": [],
            "calls": 0, "rows_stored": 0, "rows_rejected": 0,
            "batches_failed": 0, "status": "running",
        }
        _save_state_best_effort(conn, state)
    else:
        state["status"] = "running"  # resumed sweep is active again
        _save_state_best_effort(conn, state)

    limit = max_batches if max_batches is not None else MAX_BATCHES_PER_RUN
    done_this_run = 0
    consecutive_failures = 0
    stop_reason = None

    for idx in pending_batches(state):
        if done_this_run >= limit:
            stop_reason = "batch_limit"
            break
        batch = batches[idx]
        res = fetch_json(session, FORECAST_URL, params=build_params(batch),
                         timeout=120)
        state["calls"] += 1
        if not res.get("ok"):
            state["batches_failed"] += 1
            state["last_error"] = str(res.get("error"))[:200]
            # fetch_json reports an exhausted 429 as status_code=None +
            # error "HTTP 429" (its retry loop falls through), so check
            # both shapes.
            if res.get("status_code") == 429 or \
                    str(res.get("error")).startswith("HTTP 429"):
                state["status"] = "stopped_429"
                stop_reason = "http_429"
                _save_state_best_effort(conn, state)
                break
            consecutive_failures += 1
            _save_state_best_effort(conn, state)
            # Per-batch progress on stdout (added 2026-10-18): stage
            # output tails previously showed only the final JSON "}",
            # making bulk exit-1s undiagnosable from pass reports.
            print(f"bulk batch {idx + 1}/{total_batches} FAILED "
                  f"err={state['last_error'][:80]} "
                  f"completed={len(state['completed'])} "
                  f"rows={state['rows_stored']} "
                  f"failed_total={state['batches_failed']}", flush=True)
            if consecutive_failures >= STOP_AFTER_CONSECUTIVE_FAILURES:
                state["status"] = "stopped_errors"
                stop_reason = "consecutive_failures"
                _save_state_best_effort(conn, state)
                break
            continue
        consecutive_failures = 0
        readings = parse_bulk_response(res.get("data"), batch)
        fetched_at = utc_iso()
        valid = []
        for reading in readings:
            if not reading_in_range(reading):
                state["rows_rejected"] += 1
                continue
            valid.append((reading["geoname_id"], reading["observed_at"],
                          reading["temperature"], reading["humidity"],
                          reading["pressure"], reading["wind_speed"],
                          reading["precipitation"], reading["weather_code"],
                          fetched_at))
        before = conn.total_changes
        if not _wait_for_collector_window(conn):
            state["status"] = "deferred"
            stop_reason = "deferred_collector_busy"
            print(f"bulk deferred at batch {idx + 1}: collector busy "
                  f"through the whole window; checkpoint kept, next "
                  f"pass resumes", flush=True)
            break

        def _insert_batch() -> None:
            conn.executemany(
                """INSERT OR IGNORE INTO global_place_weather
                   (geoname_id, observed_at, temperature, humidity, pressure,
                    wind_speed, precipitation, weather_code, fetched_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""", valid)
            conn.commit()

        _write_with_retry(_insert_batch)
        state["rows_stored"] += conn.total_changes - before
        state["completed"] = sorted(set(state["completed"]) | {idx})
        done_this_run += 1
        _save_state_best_effort(conn, state)
        print(f"bulk batch {idx + 1}/{total_batches} ok "
              f"completed={len(state['completed'])} "
              f"rows={state['rows_stored']} "
              f"rejected={state['rows_rejected']} "
              f"failed_total={state['batches_failed']}", flush=True)
        if sleep_s:
            time.sleep(sleep_s)

    if len(state["completed"]) >= total_batches:
        state["status"] = "complete"
    elif state.get("status") == "running":
        state["status"] = "partial"
    _save_state_best_effort(conn, state)

    summary = dict(state)
    summary["completed_batches"] = len(state["completed"])
    summary["stop_reason"] = stop_reason
    log.info("global sweep: %s", {k: summary[k] for k in (
        "status", "completed_batches", "total_batches", "calls",
        "rows_stored", "rows_rejected", "stop_reason")})
    return summary
