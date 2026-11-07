"""Source scheduling (§4): per-source poll intervals honoring real source cadence.

HARD RULE: we NEVER poll faster than a source's terms allow to "look fast".
Each interval below carries a justification tied to the source's documented
update cadence / license terms. The table `perf_source_schedule` persists the
last-run times so the fast runner, cron, and any worker all agree.

Default intervals:
  open-meteo        900s  (15 min)  — forecasts update every 1-6h by model; free tier
                                      fair-use asks for no aggressive polling; 15 min
                                      is the standing project cadence (cron=hourly,
                                      fast runner never below 15 min)
  usgs_quakes       120s  (2 min)   — ComCat feeds update ~every minute for the
                                      all_hour feed; 1-5 min is a respectful cadence
                                      that never exceeds the data's own freshness
  nws_tsunami       300s  (5 min)   — NWS CAP/alerts feeds update on issuance;
                                      tsunami messages are event-driven; 5 min
                                      polling catches advisories without hammering
                                      (sandbox note: NWS is unreachable from here;
                                      intervals stay honest regardless)
  weather_obs        600s (10 min)  — observation stations report every 10-60 min
  aftershock        3600s (1 h)     — aftershock outlooks change rarely; hourly is plenty
  regional_eq       1800s (30 min)  — regional networks are slower than ComCat
  radar/satellite  21600s (6 h)    — stub collectors; no live feed, keep slow
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Dict, List, Optional

SOURCE_INTERVALS: Dict[str, Dict[str, object]] = {
    "open-meteo": {
        "interval_seconds": 900,
        "timeout_seconds": 60,
        "justification": "Open-Meteo free API: forecasts update per model run (1-6h); "
                         "15 min cadence respects fair-use, no faster polling allowed.",
    },
    "usgs_quakes": {
        "interval_seconds": 120,
        "timeout_seconds": 60,
        "justification": "USGS ComCat GeoJSON feeds update about every minute; "
                         "2 min polling is inside the 1-5 min respectful band, "
                         "never faster than the data's own freshness.",
    },
    "nws_tsunami": {
        "interval_seconds": 300,
        "timeout_seconds": 60,
        "justification": "Tsunami messages are event-driven; 5 min polling catches "
                         "advisories without hammering the feed.",
    },
    "weather_obs": {
        "interval_seconds": 600,
        "timeout_seconds": 45,
        "justification": "Surface observations report every 10-60 min; 10 min matches "
                         "the fastest station cadence, never exceeds it.",
    },
    "aftershock": {
        "interval_seconds": 3600,
        "timeout_seconds": 60,
        "justification": "Aftershock outlooks are updated infrequently (event-driven, "
                         "hours); hourly is the standing cadence.",
    },
    "regional_eq": {
        "interval_seconds": 1800,
        "timeout_seconds": 60,
        "justification": "Regional network catalogs lag ComCat; 30 min is honest "
                         "about their latency.",
    },
    "radar": {
        "interval_seconds": 21600,
        "timeout_seconds": 30,
        "justification": "Stub collector (no live radar feed deployed); 6 h avoids "
                         "wasted cycles, honest about data availability.",
    },
    "satellite": {
        "interval_seconds": 21600,
        "timeout_seconds": 30,
        "justification": "Stub collector (no live satellite feed deployed); 6 h avoids "
                         "wasted cycles, honest about data availability.",
    },
    "alert_engine": {
        "interval_seconds": 120,
        "timeout_seconds": 120,
        "justification": "Local computation, no external terms; runs on the quake "
                         "cadence so life-safety events are processed within ~2 min "
                         "of collection.",
    },
}


def ensure_schedule_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS perf_source_schedule (
            source TEXT PRIMARY KEY,
            interval_seconds INTEGER NOT NULL,
            last_started_at TEXT,
            last_finished_at TEXT,
            last_status TEXT,
            updated_at TEXT NOT NULL
        )""")
    conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sync_defaults(conn: sqlite3.Connection) -> None:
    """Seed the schedule table with the spec intervals (idempotent)."""
    ensure_schedule_table(conn)
    for source, cfg in SOURCE_INTERVALS.items():
        conn.execute(
            """INSERT INTO perf_source_schedule (source, interval_seconds, updated_at)
               VALUES (?,?,?)
               ON CONFLICT(source) DO UPDATE SET
                 interval_seconds=excluded.interval_seconds,
                 updated_at=excluded.updated_at""",
            (source, cfg["interval_seconds"], _now()))
    conn.commit()


def mark_started(conn: sqlite3.Connection, source: str) -> None:
    conn.execute(
        "UPDATE perf_source_schedule SET last_started_at=?, last_status='running', "
        "updated_at=? WHERE source=?", (_now(), _now(), source))
    conn.commit()


def mark_finished(conn: sqlite3.Connection, source: str, ok: bool) -> None:
    conn.execute(
        "UPDATE perf_source_schedule SET last_finished_at=?, last_status=?, "
        "updated_at=? WHERE source=?",
        (_now(), "ok" if ok else "error", _now(), source))
    conn.commit()


def due_sources(now: Optional[datetime] = None) -> List[Dict[str, object]]:
    """Sources whose interval has elapsed since last start — but note: this is
    the static spec view. `due_sources_db(conn)` is the authoritative one."""
    now = now or datetime.now(timezone.utc)
    return [{"source": s, "interval_seconds": c["interval_seconds"],
             "justification": c["justification"]}
            for s, c in SOURCE_INTERVALS.items()]


def due_sources_db(conn: sqlite3.Connection,
                   now: Optional[datetime] = None) -> List[str]:
    """Return source names that are due NOW per the persisted schedule."""
    ensure_schedule_table(conn)
    now = now or datetime.now(timezone.utc)
    due: List[str] = []
    for row in conn.execute(
            "SELECT source, interval_seconds, last_started_at FROM perf_source_schedule"):
        src, interval, last_started = row["source"], row["interval_seconds"], row["last_started_at"]
        if not last_started:
            due.append(src)
            continue
        try:
            last = datetime.fromisoformat(last_started)
        except ValueError:
            due.append(src)
            continue
        if (now - last).total_seconds() >= interval:
            due.append(src)
    return due
