# Operations

## API platform (v2)

- **Rate limiting** (§77): `backend/api/middleware.py` — in-memory token
  bucket per client IP, applied to `/v1/*` only. Default quota
  `AETHER_RATE_LIMIT_RPM=120` requests/minute/IP
  (`AETHER_RATE_LIMIT_BURST`, default = same). Over-quota requests get
  HTTP 429 + `Retry-After` header. `/health`, `/status`, `/metrics` are
  exempt (ops paths must stay reachable). State is per-process; on this
  single-host deployment that is the whole fleet.
- **API-key auth hook** (§66): `backend/api/auth.py` — routes opt in with
  `Depends(require_api_key)`, header `X-API-Key`. **Disabled by default**
  (API is open); set `AETHER_API_KEY` to a non-empty value to enable.
  No user accounts — one shared service key for operator tooling.
- **Response envelope** (§78): new v2 endpoints wrap payloads with
  `backend/api/envelope.py::envelope()` →
  `{data, source, source_timestamp, retrieved_at, processed_at,
  model_version, confidence, data_quality, status}`. Existing v1 shapes
  are pinned by `tests/test_api_contract.py` and are not rewrapped.
- **Pagination** (§77): `backend/api/pagination.py::paginate()` —
  `page_size` hard-capped at 500, `page` clamped to ≥ 1.
- **Logging**: plain-text lines by default (`backend/common/logging_setup.py`);
  set `AETHER_LOG_JSON=1` for one-JSON-object-per-record output. The API
  request-logging middleware emits method/path/status/duration/client_ip
  per request.
- **v2 route wiring**: `backend/api/app.py` auto-includes any present
  `backend/api/routes/{nowcast,engines,notify,trust,admin,replay}` module
  exposing `router`; missing modules are skipped so parallel workstream
  landings never break the app.

## New environment variables (v2)

| Variable | Default | Purpose |
|---|---|---|
| `AETHER_RATE_LIMIT_RPM` | `120` | Token-bucket refill per minute per IP (`/v1/*`) |
| `AETHER_RATE_LIMIT_BURST` | = RPM | Bucket capacity (burst allowance) |
| `AETHER_API_KEY` | *(unset = disabled)* | Enables `X-API-Key` auth on opted-in routes |
| `AETHER_LOG_JSON` | `0` | `1` → structured JSON log records |
| `AETHER_LOG_LEVEL` | `INFO` | Log level |

## Data retention policy (§69 — cost governor)

Raw tables grow on every poll; `scripts/prune.py` enforces retention.
**Dry run by default** — pass `--apply` to delete, `--table <name>` to
scope. The scientific record and reproducibility artifacts are **never**
auto-pruned.

| Table | Time column | Keep | Notes |
|---|---|---|---|
| `weather_forecasts` | `valid_time` | **never auto-pruned** | ML training data — owner rule 2026-10-16 ("Training data မဖျက်နဲ့"). A 45-day window was adopted and reversed the same day before any row was deleted; the `aether-prune` cron was removed. DB growth is accepted; manage capacity, never training data. |
| `weather_observations` | `observed_at` | **never auto-pruned** | ML training data — same owner rule as forecasts. |
| `system_health` | `checked_at` | 180 days | Ops telemetry |
| `data_quality` | `checked_at` | 180 days | v2 check log |
| `ingestion_runs` | `started_at` | 180 days | v2 run ledger |
| `notification_history` | `created_at` | 2 years | + cascades to `notification_deliveries` |
| `alert_revisions` | `changed_at` | 2 years | Alert audit trail |
| `earthquake_events`, `earthquake_updates`, `tsunami_events`, `tsunami_messages`, `earth_events`, `alerts`, `model_versions`, `model_scores`, `training_runs`, `locations`, `users`, `saved_locations`, `source_registry` | — | **never auto-pruned** | Scientific record; ML reproducibility |

Before pruning ages out ML training inputs, export the training snapshot to
Drive `TRAINING_DATA/` (the ML team's snapshot tooling) — pruned raw rows
cannot be reconstructed from Sheets/Drive mirrors otherwise.

## Daily checks

1. `/v1/health` (or the `health-check` workflow): every collector's
   `system_health` row should be `ok` or `degraded` with a recent
   `last_success`. Anything `down` needs attention (see TROUBLESHOOTING.md).
2. `validate-data` workflow result (daily 02:00 UTC): data-quality failures
   are real and should be investigated, not re-run blindly.
3. Disk: `data/aether.db` grows with every poll; watch `data/backups/` too.

## Google Sheets mirror (optional)

Enable with the connected Google account (`hatch_gws_cli sheets status` → connected)
and a spreadsheet id: `AETHER_SHEETS_SPREADSHEET_ID` env or `bootstrap/SHEETS_ID.txt`.
No service-account keys are used. Behavior, from `backend/integrations/sheets.py`:

- `SheetMirror()` reads env at construction; missing creds → disabled.
- `ensure_tabs()` creates the nine control tabs if absent:
  `weather_data, observations, earthquake_events, tsunami_events,
  forecast_errors, model_scores, alerts, training_runs, source_health`.
- `push_summary(table_name, rows)` maps SQLite table names to tabs
  (`weather_forecasts → weather_data`, `source_registry → source_health`,
  etc.) and rewrites the tab with the summary batch.
- `health()` → `not_configured` (no creds), `ok` (reachable), `down`.
- Sheets is a **control/metadata mirror only** — never the primary DB.

## Google Drive archive (optional)

Same connected-account transport (+ optional `AETHER_DRIVE_ROOT_FOLDER_ID`
or `bootstrap/DRIVE_ROOT_ID.txt`).
From `backend/integrations/drive.py`:

- `ensure_folders()` creates
  `AETHER_EARTH_INTELLIGENCE/RAW/{WEATHER,EARTHQUAKE,TSUNAMI,SATELLITE,RADAR}`,
  `PROCESSED, TRAINING_DATA, MODELS, VALIDATION, ALERT_ARCHIVE, REPORTS,
  BACKUPS`.
- `upload_parquet_or_json(local_path, category)` archives a file under the
  matching `RAW/<CATEGORY>` folder. The `backup` workflow uses it for the
  daily SQLite copy.
- Drive is an **archive mirror only** — it cannot reconstruct the database.

## Backups

- `backup` workflow (daily 05:00 UTC): copies `data/aether.db` to
  `data/backups/aether-<UTC timestamp>.db`, then uploads via DriveArchive
  (skipped with a clear message when Drive is not configured — the local
  copy is still kept).
- Rotation: keep 30 daily copies locally, then prune oldest (manual until
  the backend team automates it).
- Restore: stop collectors, replace `data/aether.db` with the backup copy,
  restart. WAL files (`-wal`, `-shm`) must be restored together or deleted
  before opening.

## User data requests

Data lives in local SQLite. Export: `SELECT` the user's rows from
`saved_locations` joined to `locations`. Delete: delete the rows. (Exact API
support pending from the backend team.)

## Observability

- `/health` → `{"status": "ok", ...}` liveness probe (no DB read).
- `/status` → per-table row counts + latest `system_health` check +
  provenance; the human-readable overview.
- `/metrics` → Prometheus text: `aether_table_rows{table=...}` gauges and
  `aether_collector_status{component=...}` (1=ok, 0=not ok).
- Request logs: one line per request (method/path/status/duration/ip);
  JSON with `AETHER_LOG_JSON=1`.
- `system_health` table is the collector telemetry store; the
  `health-check` workflow reads it. v2 additions log there too
  (`db:migrate_v2` marker, `data_quality` rows from `validate-data`).

## Honest limitations

- There is no automated failover and no multi-host replication; the backup
  file plus the Drive copy is the disaster story.
- Mirror writes are summaries, not transactions: a Sheets tab can lag the DB
  and that is by design (control mirror, not replica).
- As of 2026-10-05 the backup workflow had never run against a real
  populated database; the first scheduled run is its first exercise.
