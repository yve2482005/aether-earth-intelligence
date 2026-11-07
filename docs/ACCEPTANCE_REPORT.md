# AETHER Performance Track — Acceptance Report

Perf spec §§84–122. Generated from automated acceptance tests
(`tests/test_acceptance_*.py`); every number below is a real measurement from
that run. PASS/FAIL/BLOCKED per §120. Verdict counts:
PASS=32 FAIL=1 BLOCKED=3.

Honesty notes: synthetic fixtures are labeled SYNTHETIC and run in temporary
databases — never production tables or real alert channels. Redis/PostGIS are
documented future plug-ins, never claimed running. External latency is outside
our control (§60); all latencies are measured, never fabricated.

### Performance

- **84.1 build / migrations / startup** — **PASS**
  - evidence: migrate_tables=11; migrate_indexes=17; routes_mounted=9; startup_import_ms=502.7
- **85 ingestion per-source metrics** — **PASS**
  - evidence: per_source_latency_ms={'usgs_quakes': 122.9, 'open-meteo': 205.6, 'nws_tsunami': 83.1, 'weather_obs': 155.4}; wall_ms=210.0; sources=4
- **87 parallel vs sequential benchmark** — **PASS**
  - evidence: parallel_ms=305.7; sequential_ms=1200.5; speedup=3.93
- **88 cache hit rate** — **PASS**
  - evidence: requests=50; hit_rate=0.98; fetch_executions=1
- **89 100-request single-flight coalescing** — **PASS**
  - evidence: requests=100; fetch_executions=1; coalesced=99
- **90 API p50/p95/p99** — **PASS**
  - evidence: n=60; p50_ms=14.0; p95_ms=34.3; p99_ms=59.0
- **91 home bundle timing** — **PASS**
  - evidence: build_ms=9.1; store_ms=3.0; cached_serve_ms=0.3
- **92 DB query latency + EXPLAIN plans** — **PASS**
  - evidence: queries=5; indexed=5; latencies_ms={'quake time-range scan': 0.68, 'quake magnitude filter': 1.49, 'alert status scan': 0.13, 'forecast bundle fetch': 0.68, 'health freshness lookup': 0.1}
- **93 map viewport/clustering caps** — **PASS**
  - evidence: events_considered=5000; clustered_returned=15; individual_returned=2000; cap=2000; truncated_individual=True
- **94 ML batch vs single inference** — **PASS**
  - evidence: rows=64; batch_ms=4106.2; single_loop_ms=71279.7; speedup=17.36
- **95 precomputation reuse + invalidation** — **PASS**
  - evidence: reuse=True; invalidation_on_rebuild=True; stale_served=False
- **98 alert stage latency, P95 source-to-alert <10s** — **PASS**
  - evidence: synthetic=True; n=10; p95_source_to_alert_ms=20.3; target_ms=10000
- **100 P0 ahead of P3 queue ordering** — **PASS**
  - evidence: p3_queued=5; first_served=P0 life-safety
- **106 observability metrics + trace IDs** — **PASS**
  - evidence: stages=['alert', 'fetch']; trace_id_len=16; persisted_keys=2
- **107 resource bounds** — **PASS**
  - evidence: metric_ring_bound=2048; map_feature_cap=2000; connections=thread-local, one per thread
- **108 load: 5xx < 1%** — **PASS**
  - evidence: requests=50; non_200=0; error_rate=0.0
- **109 concurrency** — **PASS**
  - evidence: concurrent_threads=50; all_ok=True
- **110 regression vs baseline (>20% degradation = FAIL)** — **FAIL**
  - evidence: baseline_p50_ms=0.109; current_p50_ms=0.173; drift=0.588; threshold=0.2; method=direct min-of-3 round medians (harness noise + transient VM contention excluded); round_medians_ms=[0.175, 0.178, 0.173]
  - FAIL detail: metric=home_bundle_cached_p50_ms actual=0.173 target=0.131 difference=58.8 cause=regression in bundle serve path — investigate fix=profile precompute.get_bundle; check DB growth/indexes

### Data Quality

- **86 incremental ingestion, duplicate_event_rate=0%** — **PASS**
  - evidence: ingested=10; re_poll_new=0; duplicate_event_rate=0.0
- **99 dedup duplicate_notification_rate=0%** — **PASS**
  - evidence: deliveries=2; notifications_created=1; duplicate_notification_rate=0.0
- **101 failure recovery** — **PASS**
  - evidence: recovered_after=cooldown + half-open probe; final_state=closed
- **102 circuit breaker** — **PASS**
  - evidence: opened_after=3; failures=consecutive; half_open_probe=True
- **103 freshness exposure (§66)** — **PASS**
  - evidence: status=could_not_verify; wording=CURRENT STATUS COULD NOT BE VERIFIED
- **104 offline behavior** — **PASS**
  - evidence: sources=2; all_failed=True; runner_crashed=False; honest_errors=True
- **105 validation never skipped for speed** — **PASS**
  - evidence: rows=2; valid=1; excluded_invalid=1; validation_skipped=False
- **114 security: no secrets anywhere** — **PASS**
  - evidence: files_scanned=34; hits=0
- **115 provenance on everything** — **PASS**
  - evidence: provenance_sources=['aether-db']; freshness_blocks=['weather', 'earthquake', 'tsunami', 'bundle_built_at']

### Forecast Quality

- **111 accuracy separated from speed** — **PASS**
  - evidence: accuracy_metric=mae; mae=1.1574; source=model manifest (validation, real validation pairs); speed_source=perf metrics accumulators (runtime)
- **112 accuracy metrics not conflated** — **PASS**
  - evidence: data_kind=real; accuracy_claim=real validation pairs — measured validation metrics, never presented as guaranteed real-world skill

### Alert Quality

- **96 synthetic quake e2e + M5.8->M6.1 revision versioning** — **PASS**
  - evidence: synthetic=True; below_threshold_alerts=0; after_revision_alerts=1; rerun_new_alerts=0; alert_rows=1; version=1
- **97 synthetic tsunami fixtures, no invented arrival times** — **PASS**
  - evidence: synthetic=True; arrival_time=None; invented=False
- **113 alert precision/recall (synthetic)** — **PASS**
  - evidence: synthetic=True; n_events=6; precision=1.0; recall=1.0
- **118 full synthetic end-to-end source->notification** — **PASS**
  - evidence: synthetic=True; stages=source->validate->dedup->severity->alert->P0 queue; alert_latency_ms=22.9; queued_id=1; delivery=held (FCM unconfigured) — never faked

### Environment-blocked items (not failures)

- **92.5 PostGIS plan comparison (SQLite deployment)** — BLOCKED: PostGIS. deployment DB is SQLite; PostGIS query plans cannot be measured here Workaround: documented as production plug-in in docs/PERFORMANCE.md
- **116 UI performance** — BLOCKED: UI workstream (redesign track) + real device/WebView. UI performance is owned by the UI workstream; this environment has no WebView/device to measure render or interaction latency Workaround: measure only API-side timings here (see 90/91); UI measurements belong to the UI track's device testing
- **117 accessibility** — BLOCKED: UI workstream (redesign track). accessibility audits require the rendered UI; owned by the UI workstream Workaround: API responses here are plain JSON with no UI claims

### Release gate (§121)

Zero-defect checks:
- 0 duplicate critical alerts: PASS
- 0 fabricated warnings (no invented arrival times, no skipped validation): PASS
- 0 exposed secrets: PASS
- provenance preserved: PASS

**Release gate: GATED — not production-ready: 110 regression vs baseline (>20% degradation = FAIL)**

### What was NOT tested (honest limits)

- Real FCM delivery (no device tokens; held honestly, never faked)
- PostGIS query plans (deployment is SQLite)
- Multi-host / distributed behavior (single-host SQLite model)
- Adversarial / high-throughput load (moderate concurrency only: 20–100 threads)
- UI performance + accessibility (owned by UI workstream)
- Live source endpoints from sandbox (no network: USGS ETag check skipped)
- 1M-record map benchmarks (VM-sized: 5000-event cap test used)
