# Troubleshooting

## Collector failures

| Symptom | Likely cause | Fix |
|---|---|---|
| `system_health` shows `down`, `last_failure` recent | upstream endpoint unreachable or rate-limited | check the endpoint manually; NWS 403 almost always means the User-Agent header is missing |
| Open-Meteo 429 | over the ~10k/day fair use | reduce poll frequency; the 30-min schedule is already conservative |
| `data/aether.db` missing | `scripts/init_db.py` never ran | run it; workflows fail with an explicit message until then |
| Duplicate earthquake events | dedup windows too tight or `source_event_ids` not merged | check `event_deduplicator.py` against the rule pinned in `tests/test_dedup.py` |
| Duplicate alerts | `dedup_key` collision or re-issue with a new key | inspect the `alerts` table; re-issues with new keys are expected and kept as history |

## Sheets / Drive mirror issues

| Symptom | Likely cause | Fix |
|---|---|---|
| health = `not_configured` | env vars not set | set `AETHER_GOOGLE_CREDENTIALS_JSON` (+ spreadsheet id); this is the normal state when mirrors are unwanted |
| health = `down`, "invalid JSON" | credential env var malformed | fix the JSON; the value is never logged — check your secrets store |
| health = `down`, auth errors | service account lacks access or wrong spreadsheet id | share the spreadsheet with the service-account email; verify the id |
| `push_summary` raises ValueError | unknown table name | use one of the mapped names (`TAB_MAP` in `sheets.py`) |
| ImportError for gspread / google-api-python-client | optional deps not installed | `pip install -r requirements.txt`; the modules degrade gracefully without them |

## API issues

| Symptom | Likely cause | Fix |
|---|---|---|
| `/v1/*` missing `provenance` | backend regression | contract test `test_api_contract.py::test_responses_carry_provenance` catches this in CI |
| 404 on documented route | route not registered | `test_all_contract_routes_registered` lists the missing ones |
| stale data, large `data_age_min` | collectors not running | check the `collect-*` workflow runs and `system_health` |

## Test failures

- `test_honesty.py` failing on a phrase: the phrase is banned — reword the
  string (do not weaken the test).
- `test_security.py` failing on a pattern: a secret-looking value entered
  the repo — remove it, rotate the credential, check git history.
- `test_dedup.py` / `test_alerts.py` failing against backend code: the
  backend implementation drifted from the pinned contract — fix the code,
  not the contract, unless the contract itself was renegotiated.

## Honest limitations

- This guide covers the components that exist. For behavior not yet
  observed in operation (live collector runs, trained models, on-device
  testing) the correct first step is "check the status table in README.md"
  — those are the known open gaps.
