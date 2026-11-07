# AETHER Performance Track — PERFORMANCE.md

How the v2 performance track works, what was measured, and what was not.
All numbers are real measurements from `tests/test_acceptance_*.py`
(see [ACCEPTANCE_REPORT.md](ACCEPTANCE_REPORT.md)). Nothing here is estimated.

## Architecture

```
sources ──► ParallelCollector (ThreadPoolExecutor, per-source circuit breakers)
                │ fast path: usgs → validate → dedup → severity → alert engine → notify queue (P0)
                │ slow path (background threads, completion-logged): verify reports
                │   (training/backtest/Drive backup: not wired — must not run partially)
                ▼
schedule (per-source cadence, persisted) → due_sources_db()
                ▼
incremental (ETag/Last-Modified + cursors) → ids (sha256 content-hash dedup)
                ▼
SQLite (WAL) + perf indexes → cache (SQLite TTL; Redis = future plug-in)
                ▼
precompute (home bundles) → API: /v1/home/{id}, /v1/map/events, /v1/perf/*
                ▼
metrics (per-stage p50/p95/p99, trace IDs) → GET /v1/perf/metrics
notify_queue (persistent lanes, defensive FCM bridge)
ml_batch (batch feature assembly → one predict/model; never trains on request path)
geo (H3 cells for locations + map clustering)
```

## Measured results (acceptance run, 2026-10-05)

| Area | Measurement |
|---|---|
| Parallel vs sequential (4×300 ms sources) | 307 ms vs 1201 ms — **3.91× speedup** |
| Per-source latencies (simulated) | recorded per source into metrics (e.g. 121/202/84/156 ms), wall 204 ms |
| Cache hit rate (50 reqs) | 98% (1 fetch execution) |
| Single-flight coalescing (100 concurrent) | 1 fetch execution, 99 coalesced |
| /v1/home p50/p95/p99 (n=60, via TestClient) | 5.2 / 9.8 / 13.8 ms |
| Home bundle: build / cached serve | 4.8 ms / 0.2 ms |
| DB hot queries (200-row table) | worst 0.89 ms; 5/5 use indexes (EXPLAIN QUERY PLAN) |
| Map: 5000 events → clustered / individual | 15 cells / 2000 features (hard cap, truncated) |
| ML batch 64 rows vs 64 single predicts | 17.1 ms vs 498.7 ms — **29.2×** |
| Alert stage P95 (synthetic) | 5.8 ms (< 10 s target) |
| Incremental re-poll duplicate_event_rate | 0.0 |
| Duplicate notification rate | 0.0 |
| Load: 50 concurrent, non-200 rate | 0.0% |
| Secrets scan (new files) | 0 hits |
| Regression gate (110) | min-of-3 round medians, warmup; 20% threshold on direct-method serve |

Measurement honesty notes: the TestClient/httpx shim in this venv adds
~10–16 ms/request of harness overhead (measured on a trivial endpoint), so
sub-ms API gates measure the route's core work directly and say so (test 110
uses warmup + min-of-3 round medians to discount transient VM contention;
a real regression shifts all rounds).

## Schedules & source terms (§4)

| Source | Interval | Justification |
|---|---|---|
| open-meteo | 15 min | free API fair-use; model runs are 1–6 h; never below 15 min |
| usgs_quakes | 2 min | ComCat updates ~1 min; 1–5 min band, never faster than the data |
| nws_tsunami | 5 min | event-driven messages; catches advisories without hammering |
| weather_obs | 10 min | stations report every 10–60 min |
| aftershock | 1 h | outlooks change rarely |
| regional_eq | 30 min | regional catalogs lag ComCat |
| radar / satellite | 6 h | stub collectors, no live feed — honest slow cadence |
| alert_engine | 2 min | local compute; matches quake cadence |

## Priority lanes (§5)

P0 life-safety > P1 real-time hazards > P2 normal > P3 background.
Starvation guard: every 10th serve goes to the lowest non-empty lane.
Wiring point into the alert engine (owned by alerting team, not edited here):
`lane_for_severity()` maps critical/warning→P0, watch→P1, advisory/info→P2,
else P3 — call it from the alert dispatch path and enqueue a `Task`.

## Cache rules (§§12–13)

- SQLite-backed TTL store (`cache_entries`). Redis exists only as
  `RedisCache`, an interface stub that raises — **never claimed running**.
- Stale-while-revalidate: non-critical data only.
- Life-safety paths: bypass cache or `strict=True`; on failure return
  `could_not_verify` and show **"CURRENT STATUS COULD NOT BE VERIFIED"** (§66).

## Deployment model, stated honestly (§§31–32, 60)

- **Single host, SQLite (WAL), thread-local connections.** This does not
  scale past one host; the multi-host path is a client/server DB.
- **No PostGIS here.** H3 indexing uses the `h3` Python package; PostGIS is
  the documented scaling path, not deployed.
- **No Redis here.** Documented future plug-in only.
- **No universal millisecond guarantees** (§60): external source latency is
  outside our control; we measure and report it, never promise it.
- TestClient/httpx-shim overhead in this venv is ~10–16 ms/request; API
  latency gates that need sub-ms precision measure the route's core work
  directly (see test 110) and say so.

## What was NOT tested

- Real FCM delivery (no device tokens — queue holds honestly, never fakes)
- PostGIS query plans (SQLite deployment)
- Multi-host / distributed behavior
- Adversarial or high-throughput load (moderate concurrency: 20–100 threads)
- UI performance / accessibility (UI workstream owns these)
- Live source endpoints from the sandbox (no network; USGS ETag check skipped)
- 1M-record map benchmarks (VM-sized: 5000-event cap test)

## Wiring checklist for integration

1. `backend/api/app.py`: append `"perf"` to `_V2_ROUTE_MODULES` (workstream E,
   one line) to mount the perf router automatically.
2. Alert engine → priority queues: call `lane_for_severity()` + enqueue a
   `Task` at the alert dispatch site (alerting team).
3. Cron: run `scripts/collect_fast.py` every ~2 min for the fast path;
   `scripts/run_collect.py` (hourly) is untouched.
4. Slow jobs: `run_slow_jobs()` launches background threads with completion
   logging to `perf_slow_jobs`.

## Files (workstream F owns)

`backend/perf/` (12 modules), `backend/db/migrate_perf.py`,
`backend/api/routes/perf.py`, `scripts/collect_fast.py`,
`tests/test_perf_*.py`, `tests/test_acceptance_*.py`,
`docs/PERFORMANCE.md`, `docs/ACCEPTANCE_REPORT.md`.
