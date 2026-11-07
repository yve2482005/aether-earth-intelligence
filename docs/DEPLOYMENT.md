# Deployment

## Prerequisites

- Python 3.12, `pip install -r requirements.txt` (all free packages).
- Zero spend: no paid services are required at any step.
- Optional: Google service-account JSON for the Sheets/Drive mirrors
  (see OPERATIONS.md). The system runs fully without it.

## Local run

```bash
pip install -r requirements.txt
python scripts/init_db.py          # creates data/aether.db from backend/db/schema.sql
python scripts/seed_registry.py    # seeds source_registry incl. license terms
python scripts/run_collect.py weather      # one-shot collection (backend team)
python scripts/run_collect.py earthquake
python scripts/run_collect.py tsunami
python scripts/run_api.py          # FastAPI, serves /v1 (backend team)
python -m pytest tests/ -q         # test suite
```

`scripts/` is owned by the backend team and has landed (`init_db.py`,
`run_collect.py`, `run_api.py`, `seed_registry.py`). If a script is missing,
the GitHub workflows fail with an explicit "not landed yet" message rather
than silently doing nothing.

## Scheduled operation (GitHub Actions)

Nine workflows in `.github/workflows/`:

| Workflow | Schedule | Does |
|---|---|---|
| collect-weather | every 30 min | `run_collect.py weather` |
| collect-earthquake | every 15 min | `run_collect.py earthquake` |
| collect-tsunami | every 10 min | `run_collect.py tsunami` |
| process-data | hourly | processing stage |
| validate-data | daily 02:00 UTC | data-quality validation |
| train-models | weekly Sun 03:00 UTC | `ml/weather/train.py` |
| evaluate-models | weekly Sun 04:00 UTC | `evaluate.py` + `backtest.py` |
| backup | daily 05:00 UTC | SQLite copy to `data/backups/` + Drive archive |
| health-check | every 30 min | DB presence + Sheets/Drive health |

Every workflow also runs the full pytest suite plus the ML leakage tests
(required pass when the ML team lands them). Workflows fail honestly on
error — no `|| true` masking.

## Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `AETHER_GOOGLE_CREDENTIALS_JSON` | no | service-account JSON → enables Sheets + Drive |
| `AETHER_GOOGLE_CREDENTIALS_FILE` | no | path to the JSON file (alternative) |
| `AETHER_SHEETS_SPREADSHEET_ID` | no | target spreadsheet (required when creds set) |
| `AETHER_DRIVE_ROOT_FOLDER_ID` | no | target Drive folder (created if absent) |

With none of these set, the mirrors report `not_configured` and everything
else works normally.

## Android

The Android team builds the WebView shell (`app/`); a debug APK artifact
path is still pending from them. Server-side deployment above does not
depend on it.

## Honest limitations

- No production host is specified yet; the reference deployment is "a
  machine you control + GitHub Actions schedules".
- There is no zero-downtime story, no load balancer, no multi-region
  anything — and none is needed at this scale.
- The scripts have landed but the full sequence (init → collect → serve)
  had not been exercised end to end when this was written; the workflows
  are the first scheduled exercise.
