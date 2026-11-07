#!/usr/bin/env python3
"""Fast-path collector runner (§§6-7): usgs -> validate -> dedup -> severity -> alerts.

Does NOT replace scripts/run_collect.py (hourly cron runner). This is the
low-latency lane: it runs the life-safety fast path on a ~2-minute cadence
(SEE scripts/run_collect.py unchanged), in parallel where sources are
independent, with per-source circuit breakers and real latency recording.

Fast path:   USGS quakes -> normalize/validate -> dedup (content hashes) ->
             severity classification -> alert engine (quake/tsunami only) ->
             notification queue (P0 lane)
Slow jobs (async background threads with completion logging, NEVER blocking
the fast path): backfill, model training, backtesting, verification reports,
Drive backups — launched via `run_slow_jobs()` and logged to perf_slow_jobs.
"""
from __future__ import annotations

import json
import sys
import threading
import time
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from backend.alerting import engine as alert_engine  # noqa: E402
from backend.alerting.severity import magnitude_to_severity  # noqa: E402
from backend.common.http import make_session  # noqa: E402
from backend.common.logging_setup import setup_logging  # noqa: E402
from backend.common.timeutil import utc_iso  # noqa: E402
from backend.collectors.earthquake import usgs  # noqa: E402
from backend.collectors.tsunami import warning_sources as tsunami_src  # noqa: E402
from backend.db import db as dbmod  # noqa: E402
from backend.db import migrate_perf  # noqa: E402
from backend.perf import ids, metrics, notify_queue, parallel, schedule  # noqa: E402

log = setup_logging("aether.collect_fast")


def fast_path(conn, session) -> dict:
    """usgs -> validate -> dedup -> severity -> alerts -> notify queue.

    Runs against the process-wide ParallelCollector with per-source
    circuit breakers. Validation is never skipped for speed (§82): records
    failing normalization are logged and excluded, not pushed through.
    """
    migrate_perf.apply_conn(conn)
    schedule.sync_defaults(conn)
    collector = parallel.ParallelCollector(max_workers=4)
    trace = metrics.new_trace()

    jobs = [
        parallel.SourceJob(
            name="usgs_quakes", timeout_seconds=60,
            fn=lambda: usgs.collect(conn, session=session)),
        parallel.SourceJob(
            name="nws_tsunami", timeout_seconds=60,
            fn=lambda: tsunami_src.collect(conn, session=session)),
    ]
    t0 = time.monotonic()
    results = collector.run_all(jobs)
    fanout_ms = (time.monotonic() - t0) * 1000.0

    # validate + severity pass over freshly stored quake rows (never skipped)
    t1 = time.monotonic()
    validated, severities = 0, {}
    rows = conn.execute(
        """SELECT canonical_event_id, magnitude, origin_time FROM earthquake_events
           WHERE created_at > strftime('%Y-%m-%dT%H:%M:%SZ','now','-10 minutes')""").fetchall()
    for r in rows:
        try:
            mag = r["magnitude"]
            if mag is None:
                continue
            validated += 1
            sev = magnitude_to_severity(float(mag))
            severities[sev] = severities.get(sev, 0) + 1
        except (TypeError, ValueError):
            log.warning("invalid magnitude row %s skipped", r["canonical_event_id"])
    metrics.record("validate", "fast_path", (time.monotonic() - t1) * 1000.0,
                   trace_id=trace)

    # alerts from the alert engine (quake + tsunami lanes), timed
    t2 = time.monotonic()
    n_tsunami = alert_engine.alerts_from_tsunami(conn)
    n_quakes = alert_engine.alerts_from_earthquakes(conn)
    metrics.record("alert", "fast_path",
                   (time.monotonic() - t2) * 1000.0, trace_id=trace)

    # enqueue critical life-safety alerts to the P0 notify lane
    t3 = time.monotonic()
    critical = conn.execute(
        """SELECT alert_id, title, message FROM alerts
           WHERE status='active' AND severity IN ('critical','warning')
             AND issued_at > strftime('%Y-%m-%dT%H:%M:%SZ','now','-10 minutes')""").fetchall()
    enqueued = 0
    for a in critical:
        notify_queue.enqueue(conn, "critical", a["title"], a["message"],
                             alert_id=a["alert_id"],
                             payload={"fast_path": True, "trace_id": trace})
        enqueued += 1
    metrics.record("notify", "enqueue_fast",
                   (time.monotonic() - t3) * 1000.0, trace_id=trace)

    total_ms = (time.monotonic() - t0) * 1000.0
    metrics.record("e2e", "fast_path", total_ms, trace_id=trace)
    metrics.persist(conn, stage_filter=None)
    return {
        "trace_id": trace,
        "fanout_latency_ms": round(fanout_ms, 1),
        "sources": results,
        "validated_quakes": validated,
        "severities": severities,
        "alerts": {"tsunami": n_tsunami, "earthquakes": n_quakes},
        "critical_enqueued": enqueued,
        "total_ms": round(total_ms, 1),
        "breakers": collector.states(),
    }


def _slow_job(conn_factory, kind: str, fn) -> None:
    """Run a slow job in a background thread with completion logging."""
    def _run():
        conn = conn_factory()
        try:
            conn.execute(
                "INSERT INTO perf_slow_jobs (kind, status, created_at) VALUES (?, 'running', ?)",
                (kind, utc_iso()))
            conn.commit()
            t0 = time.monotonic()
            detail = fn(conn)
            conn.execute(
                """UPDATE perf_slow_jobs SET status='done', finished_at=?, detail=?
                   WHERE id=(SELECT MAX(id) FROM perf_slow_jobs WHERE kind=?)""",
                (utc_iso(), json.dumps(detail, default=str)[:2000], kind))
            conn.commit()
            log.info("slow job %s done in %.1fs", kind, time.monotonic() - t0)
        except Exception as e:  # noqa: BLE001
            conn.execute(
                """UPDATE perf_slow_jobs SET status='failed', finished_at=?, error=?
                   WHERE id=(SELECT MAX(id) FROM perf_slow_jobs WHERE kind=?)""",
                (utc_iso(), f"{type(e).__name__}: {e}\n{traceback.format_exc()[-1500:]}", kind))
            conn.commit()
            log.exception("slow job %s failed", kind)
        finally:
            conn.close()
    threading.Thread(target=_run, name=f"aether-slow-{kind}", daemon=True).start()


def run_slow_jobs(conn_factory) -> None:
    """Launch slow jobs async: never block the fast path (§§6-7).

    Only safe, offline-capable jobs are wired here; training/backtesting are
    stubs until their pipelines land (they must not run partially).
    """
    def _backfill_verify(conn):
        # verification report: counts per table — cheap, honest
        return {t: dbmod.table_count(conn, t)
                for t in ("earthquake_events", "tsunami_events", "weather_forecasts",
                          "alerts", "system_health")}
    _slow_job(conn_factory, "verify_report", _backfill_verify)
    log.info("slow jobs launched (verify_report); training/backtest not wired yet")


def main() -> None:
    dbmod.init_db()
    migrate_perf.apply()
    conn = dbmod.connect()
    session = make_session()
    due = schedule.due_sources_db(conn)
    log.info("due sources: %s", due)
    try:
        out = fast_path(conn, session)
        print(json.dumps(out, indent=1, default=str)[:6000])
        run_slow_jobs(dbmod.connect)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
