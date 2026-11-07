# Privacy

## What data exists

| Data | Where | Why |
|---|---|---|
| Saved locations (lat/lon, label) | local SQLite `saved_locations` | user asked to watch these places |
| User id (local random id) | local SQLite `users` | key for saved locations |
| Collected public data (weather, quakes, warnings) | local SQLite | the product itself |
| Mirror summaries (if enabled) | operator's Google Sheets/Drive | operator-configured archive |

There are no accounts with passwords, no email addresses, no payment data,
no analytics SDKs, and no ad trackers in this design.

## Where data lives

- **Primary: the operator's own host** (`data/aether.db`). Nothing leaves
  the host except:
  1. Outbound HTTPS requests to the free data sources (unavoidable — that is
     how the data is fetched; no user data is sent, only coordinates of
     watched locations as query parameters).
  2. The optional Google Sheets/Drive mirrors, **only if the operator sets
     `AETHER_GOOGLE_CREDENTIALS_JSON`**. Disabled by default; health
     reports `not_configured`.
- No cloud sync of user data is designed or implemented.

## Retention

- Forecast/observation rows accumulate by design (they are the training and
  verification record). There is currently no automatic pruning — see
  OPERATIONS.md for the manual backup/rotation procedure.
- If a user deletes a saved location, its row is deleted; derived aggregates
  (model scores) are not retroactively recomputed.

## User rights (operator guidance)

Because data stays on the operator's host, "export my data" and "delete my
data" are local SQLite operations. The API for saved locations has landed
(backend team); document the exact user-facing steps once the web team wires
them into settings.

## Honest limitations

- SQLite has no encryption at rest in the current design; anyone with host
  access can read `data/aether.db`.
- The PWA's offline cache (service worker, web team) will hold copies of
  fetched data on the user's device; device-level protections apply.
- This doc describes the intended design; the web and app teams had not
  landed their code when it was written, so client-side behavior is not yet
  verified.
