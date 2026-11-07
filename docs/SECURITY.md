# Security

## Secrets

- Credentials come **only from environment variables** (`AETHER_*`).
  There are no credentials in code, docs, configs, or git history.
- `backend/integrations/sheets.py` and `drive.py` read
  `AETHER_GOOGLE_CREDENTIALS_JSON` / `AETHER_GOOGLE_CREDENTIALS_FILE` at
  runtime, import Google client libraries lazily, and never log or print
  credential material (even in error paths — errors say "invalid JSON",
  never echo the value).
- `tests/test_security.py` scans the repo for secret-looking patterns
  (Google API keys, PEM private keys, `sk-` keys, Slack/GitHub/AWS tokens)
  and asserts the integrations source credentials only from env. It runs in
  every workflow.

## What is sensitive

- The Google service-account JSON (if the operator configures the mirrors).
  Keep it in the environment or a secrets manager, never in a file that gets
  committed. `AETHER_GOOGLE_CREDENTIALS_FILE` should point outside the repo.
- User data: `users` / `saved_locations` tables live in the local SQLite
  file. There is no account system beyond a local id, no passwords, no
  payment data anywhere in this project.

## Threat notes

- **Upstream compromise**: collectors fetch from public endpoints over
  HTTPS; `backend/processing/validation.py` (backend team) must validate
  ranges/types before storage so a malformed feed cannot corrupt the DB.
- **Dependency supply chain**: `requirements.txt` pins minimum versions;
  installs happen from PyPI in CI. No vendored wheels, no private indexes.
- **Sheets/Drive mirrors**: service-account scope is limited to
  `spreadsheets` / `drive.file` (files the app creates). The service account
  must not be granted broader Drive access.
- **Android shell**: WebView must not expose native bridges beyond what the
  app needs (Android team; see their notes in AGENTS.md).

## Incident basics

1. If a credential leaks: revoke it at the provider, rotate, and check the
   mirrors for unexpected writes.
2. If upstream data looks tampered with: quarantine the poll batch (do not
   delete — keep for forensics), mark `system_health` down, and serve the
   honest unavailable state until resolved.

## Honest limitations

- This project has not had a security audit. The measures above are baseline
  hygiene, not a certification.
- Local SQLite has no encryption at rest in the current design; physical
  access to the host means access to the data. (See PRIVACY.md for what that
  data is.)
- The Android WebView bridge surface is the Android team's responsibility
  and was not reviewed by this workstream.
