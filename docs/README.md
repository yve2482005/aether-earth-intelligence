# AETHER Docs — index

Start here, then go where you need.

| Doc | What it covers |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | System layout, data flow, component contracts |
| [DATA_SOURCES.md](DATA_SOURCES.md) | Every upstream source: endpoints, cadence, coverage |
| [DATA_LICENSES.md](DATA_LICENSES.md) | License / attribution / commercial-use / redistribution per source |
| [WEATHER_MODEL.md](WEATHER_MODEL.md) | Forecast pipeline, models, skill metrics |
| [EARTHQUAKE_SYSTEM.md](EARTHQUAKE_SYSTEM.md) | Detection, dedup, tracking, aftershock outlooks |
| [TSUNAMI_SYSTEM.md](TSUNAMI_SYSTEM.md) | Official-message-only pipeline, parsing, correlation rules |
| [ML_VALIDATION.md](ML_VALIDATION.md) | Validation protocol, leakage prevention, backtesting |
| [MODEL_CARD.md](MODEL_CARD.md) | Model card template + process for publishing one |
| [SECURITY.md](SECURITY.md) | Secrets handling, threat notes, supply chain |
| [PRIVACY.md](PRIVACY.md) | What data is collected, where it lives, retention |
| [DEPLOYMENT.md](DEPLOYMENT.md) | How to run it: local, scheduled, CI |
| [OPERATIONS.md](OPERATIONS.md) | Day-to-day ops: Sheets/Drive mirrors, backups, health |
| [TROUBLESHOOTING.md](TROUBLESHOOTING.md) | Common failures and how to diagnose them |

Also at repo root: [HONESTY.md](../HONESTY.md) (the scientific-honesty rules —
read this before touching any user-facing string) and
[CONTRACTS.md](../CONTRACTS.md) (build contracts between the teams).

## Status note

As of the evening of 2026-10-05 all workstream code has landed: backend
(collectors, processing, alerting, confidence, API, scripts), ML (models,
leakage guards, backtest, registry), web PWA (verified 10/10 per
`web/VERIFICATION.txt`), and the Android shell (debug APK at
`app/build/aether-debug.apk`). Where a doc describes behavior that has not
yet been observed in operation (live collector runs, trained champion
models, on-device testing), it says so explicitly.
