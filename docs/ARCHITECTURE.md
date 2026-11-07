# Architecture

## Big picture

```
  free upstream sources (no keys)
        │  Open-Meteo · USGS GeoJSON · NWS CAP tsunami
        ▼
 backend/collectors/{weather,earthquake,tsunami}/
        │  raw JSON → backend/processing/ (validate, normalize, dedup, align)
        ▼
 data/aether.db  (SQLite — PRIMARY store; backend/db/schema.sql is source of truth)
        │            ▲ mirrors (optional, env-gated)
        │            │ Sheets = control/metadata mirror · Drive = archive mirror
        ▼
 backend/alerting/ + backend/confidence/
        ▼
 backend/api/  (FastAPI, base /v1; every user-visible object has `provenance`)
        ▼
 web/ PWA (Burmese-first) · app/ Android WebView shell
```

`ml/` trains on SQLite snapshots and writes scores back through
`backend/ml_support/registry.py` into `model_versions`, `model_scores`, and
`training_runs`. `alerts/` re-exports `backend/alerting` (thin `__init__`
only). `gis/` is an offline gazetteer stub plus GeoJSON helpers.

## Component contracts (from CONTRACTS.md)

- **Database**: `backend/db/schema.sql` is the source of truth. Teams append
  columns only, never rename tables. All timestamps UTC ISO-8601.
- **API**: base `/v1`. Query with `lat`/`lon` or `location_id`. Responses
  carry `provenance: {sources, updated_at, data_age_min}`. Extra endpoints:
  `/health`, `/status`, `/metrics` (Prometheus text).
- **Honesty**: every datum tagged OBSERVATION / DETECTION / FORECAST /
  WARNING / RISK. Earthquake prediction is never produced. Tsunami content is
  official-messages-only. Banned phrases are enforced by tests.

### Honesty labels (the six distinct kinds)

- **OBSERVATION** — directly measured (sensor station readings).
- **DETECTION** — an event the instrument networks picked up (earthquakes).
- **FORECAST** — a model estimate of the near future, with uncertainty.
- **WARNING** — an official message from an authoritative agency, relayed
  verbatim with attribution.
- **RISK** — long-run historical/statistical hazard. Never tomorrow's events.
- **PREDICTION** — a claim about a specific future event's time/place/size.
  For earthquakes this is never produced; the label exists so the boundary
  is explicit.

See [HONESTY.md](../HONESTY.md) for the full rules.
- **i18n**: `web/i18n/{en,my,ja}.json`, flat keys, default `my`. Official
  warning categories (WARNING/ADVISORY/WATCH/INFORMATION/NO THREAT) keep their
  original wording alongside a localized explanation.
- **Secrets**: env vars only (`AETHER_*`). Never committed, never printed.

## Data flow details

1. **Collect**: `scripts/run_collect.py {weather|earthquake|tsunami}` polls
   the free endpoints on the GitHub Actions schedule
   (weather 30 min, earthquake 15 min, tsunami 10 min) and stores raw
   responses.
2. **Process**: `backend/processing/` validates ranges/units, normalizes to
   UTC + schema columns, dedups (earthquake: 120 s / 0.5° / 0.5 mag window),
   aligns forecasts to observation times.
3. **Store**: SQLite `data/aether.db`. WAL mode. Indexes on
   `(location_id, valid_time)`, `origin_time`, `start_time`, alerts status.
4. **Alert**: `backend/alerting/engine.py` ranks by `priority` (1 official
   life-safety … 5 low-confidence analysis) and `severity`
   (info/advisory/watch/warning/critical); `dedup_key` UNIQUE prevents
   duplicates; `backend/confidence/engine.py` attaches confidence labels.
5. **Serve**: FastAPI `/v1/*` with provenance on every object.
6. **Mirror** (optional): Sheets control tabs + Drive archive folders,
   enabled only when `AETHER_GOOGLE_CREDENTIALS_JSON` (+ spreadsheet/folder
   ids) is configured; otherwise health reports `not_configured`.

## v2 platform additions (workstream E)

Additive only — no v1 response shape, table, or collector behavior changed.

- **Migrations**: `backend/db/migrate_v2.py` creates the v2 tables
  (`alert_revisions`, `notification_history`, `notification_deliveries`,
  `incidents`, `devices`, `alert_preferences`, `data_quality`,
  `ingestion_runs`) with `CREATE TABLE IF NOT EXISTS` — idempotent, safe
  to run twice. Definitions are appended to `backend/db/schema.sql`, so a
  fresh `scripts/init_db.py` builds them too. Existing 16 tables untouched.
- **API foundation**: `backend/api/envelope.py` (standard v2 envelope),
  `backend/api/pagination.py` (page_size cap 500),
  `backend/api/middleware.py` (token-bucket rate limiting on `/v1/*`,
  120 req/min/IP; per-request logging),
  `backend/api/auth.py` (`X-API-Key` hook, disabled by default via
  `AETHER_API_KEY`). `app.py` wires the middleware and auto-includes any
  landed v2 route module (`nowcast`, `engines`, `notify`, `trust`,
  `admin`, `replay`); missing modules are skipped.
- **Observability**: `/health`, `/status`, `/metrics` (unchanged);
  `AETHER_LOG_JSON=1` switches logs to one-JSON-object-per-record.
- **Cost governor**: `scripts/prune.py` enforces the retention table in
  OPERATIONS.md — dry run by default, `--apply` to delete; scientific and
  reproducibility tables are never auto-pruned.
- **Queue / resilience** (§§81, 105–107): there is no message queue —
  by design (zero spend, single host). The pipeline is
  poll → validate → normalize → dedup → store → alert → serve, each
  collector isolated so one failure never crashes the others
  (`scripts/run_collect.py`). **Idempotency** is enforced at the DB layer:
  collectors write with `INSERT OR IGNORE` against UNIQUE constraints
  (e.g. `weather_forecasts` on `(location_id, source, model, forecast_run,
  forecast_hour)`, `alerts.dedup_key`, earthquake event dedup windows in
  `backend/processing/deduplication.py`) — re-running a poll never
  duplicates rows. The WAL-mode SQLite file is the single queue of record;
  Sheets/Drive are mirrors, not buffers.

## Team ownership (who writes what)

| Owner | Areas |
|---|---|
| Backend team | `backend/{common,db,collectors,processing,alerting,confidence,api}`, `scripts/` |
| ML team | `ml/`, `backend/ml_support/registry.py`, `tests/test_ml_*.py` |
| Web team | `web/`, `gis/` |
| Android team | `app/` |
| This workstream | `backend/integrations/`, `.github/`, `docs/`, `tests/` (non-ML), `README.md`, `HONESTY.md` |
| Workstream E (v2 API/DB foundations) | `backend/api/{envelope,pagination,middleware,auth}.py`, `backend/api/app.py` (v2 wiring only), `backend/db/migrate_v2.py`, `scripts/prune.py`, `tests/test_api_v2.py`, docs v2 sections |

## Honest limitations

- All workstream code has landed (backend, ML, web, app, integrations).
  What has **not** happened yet as of 2026-10-05: live collector runs
  against real endpoints, a trained/evaluated champion model, and
  on-device testing of the APK. The contracts above describe what the code
  must do; operational proof is still pending.
- The SQLite-first design means horizontal scaling is not a goal; the system
  is sized for a single host + scheduled GitHub Actions.
- Mirrors are summaries/archives, not failover: if SQLite is lost, Sheets and
  Drive do not reconstruct it — use `data/backups/` (see OPERATIONS.md).
