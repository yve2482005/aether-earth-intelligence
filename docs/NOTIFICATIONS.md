# AETHER Notifications & Alert Lifecycle (workstream C)

Push notifications, alert state machine, silent hours, personalized
thresholds, travel mode, safety/expert modes, and the daily summary.

## Architecture

```
collectors ──▶ alert engine (backend/alerting/engine.py)
                    │  state machine: DETECTED → VALIDATING → CONFIRMED
                    │  → SEVERITY_ASSESSED → ALERTED/UPDATED → RESOLVED → ARCHIVED
                    ▼
alerts table (notification_status: pending | dispatched | delivered |
              failed | not_configured | escalation_pending | suppressed_silent | no_tokens)
                    │  dispatch_pending() — honors silent hours + thresholds
                    ▼
backend/notify/fcm.py ──▶ FCM (requires credentials; never faked)
                    ▼
notification_history (landed table + read_at) — read/unread state
```

## FCM setup (exact steps)

Push is **real and config-gated**. Without configuration the system records
`not_configured` and tells the truth; it never claims a delivery that did not
happen.

1. **Firebase console** → create (or open) a project → Project Settings →
   Service accounts → **Generate new private key**. This downloads a JSON file.
   It is a secret — never commit it.
2. **Install the library** (free, zero spend — FCM itself is free):
   `~/workspace/.tooling/aether-venv/bin/pip install firebase-admin`
3. **Set the env var** to the JSON path, e.g.
   `export AETHER_FCM_CREDENTIALS_FILE=/secure/aether-fcm-service-account.json`
   (systemd/cron: put it in the service environment, not in code).
4. **Verify**: `GET /v1/notify/config` → `"fcm": {"configured": true, ...}`.
   Before that it reports exactly what is missing (env var / file / library).
5. **Register a device token**: `POST /v1/notify/tokens`
   `{user_id, token, platform, device_id?, app_version?}`. Tokens are stored in
   the `devices` table (`push_token`). Refresh via `POST /v1/notify/tokens/refresh`;
   dead tokens (`NotRegistered`) are invalidated automatically.
6. **Send**: `POST /v1/notify/send/{alert_id}?user_id=&dry_run=` or
   `POST /v1/notify/dispatch` (sends everything pending, honoring silent hours).

### Notification channels

`GET /v1/notify/channels` lists the three channels; `android_payloads` is the
verbatim JSON the Android client uses to create `NotificationChannel`s:

| channel id       | use | importance |
|------------------|-----|------------|
| `aether_safety`  | life-safety: official tsunami warnings, PAGER red/orange | high (sound+vibration, DND bypass requested) |
| `aether_hazard`  | advisories, watches, significant detections | default |
| `aether_info`    | daily summary, informational | low |

Official hierarchy always outranks AETHER analysis (§90): an official warning
goes to `aether_safety` even where an analysis of the same severity would not.

### Notification content (§102)

Every payload answers: **what** (title), **where** (region in title),
**when** (issued time), **severity**, **official?** (`OFFICIAL — <source>` vs
`AETHER`), **latest update** (revision time + revision number), **details link**
(`aether://alerts/{alert_id}` deep link, `click_action: OPEN_ALERT`).
Emergency notifications never contain unsupported predictions — no evacuation
instructions are generated unless they come from an official authority (§15).

## Android client notes

- Create the three channels from `GET /v1/notify/channels` → `android_payloads`
  on first run (`NotificationChannel`, `IMPORTANCE_HIGH` for `aether_safety`).
- Handle `click_action: OPEN_ALERT` with `data.alert_id` → open the alert detail.
- Register the FCM token with `POST /v1/notify/tokens` on every app start
  (tokens rotate); call `/tokens/refresh` when `onNewToken` fires.
- **Background-restriction caveats**: on Android 12+, exact timing cannot be
  assured when the app is force-stopped or battery-optimized; high-priority
  FCM (`priority: high` on the safety channel) wakes the app in Doze but the
  user must exempt AETHER from battery optimization for reliable delivery.
  DND bypass additionally requires the user to grant "Do Not Disturb access"
  in system settings — the app must request it, we only *ask* for the channel
  flag. The server records `delivered` only when FCM accepts the message;
  on-device display failures are outside what the server can verify.

## Alert state machine (§12)

States: `DETECTED → VALIDATING → CONFIRMED → SEVERITY_ASSESSED → ALERTED →
UPDATED → RESOLVED → ARCHIVED`. Every transition is recorded with
`(from_state, to_state, changed_at, reason, actor)`; illegal transitions raise
`InvalidTransition` instead of corrupting state. The engine bootstraps every
new alert at `DETECTED` and drives it to `SEVERITY_ASSESSED` synchronously
(detection/validation/assessment happen inside the engine run). Expiry moves
alerts to `RESOLVED`; `RESOLVED` alerts older than 30 days are `ARCHIVED`.
Rows are never deleted.

### Dedup (§13)

Alerts merge on event ID + location + time window + hazard type + source.
`M6.2 → M6.4 → M6.5` is **one** alert with revisions (the merge key excludes
severity); notification is re-armed (`escalation_pending`) only on a
*meaningful* severity step-up (e.g. LOW → HIGH). Same-severity revisions bump
the version and record history without re-notifying.

### Severity (§11)

Six-level scale: `INFORMATION / LOW / MODERATE / HIGH / SEVERE / LIFE_SAFETY`.
Legacy values map forward additively (`info→INFORMATION`, `advisory→LOW`,
`watch→MODERATE`, `warning→HIGH`, `critical→SEVERE`); `LIFE_SAFETY` is reserved
for official life-safety warnings and verified life-threatening observations.

## Silent hours (§16)

`PUT /v1/notify/preferences` with `silent_start/silent_end` (`"HH:MM"`),
`silent_days` (0=Mon..6=Sun), `silent_tz` (IANA). During silent hours ordinary
alerts are recorded as `suppressed_silent`; **life-safety alerts break through**
when `silent_override_life_safety=1` (default ON; user-configurable).

## Personalized thresholds (§48)

Per location (`PUT /v1/notify/preferences` with `location_id`): `radius_km`,
`hazard_types`, `min_severity` (v2 scale gate), `temp_high_c`, `temp_low_c`,
`rain_mm_h`, `wind_kmh`, `aqi`, `quake_mag`. The engine evaluates them against
**real collected data** (latest forecast run / detected quakes in the radius);
one alert per location+variable+day, `kind=system_analysis`, priority 4.

## Travel mode (§99)

`PUT /v1/notify/travel` with destination + lat/lon + date range; `GET` shows
status; `DELETE` disables. While active, the destination is monitored against
moderate default thresholds (nearest station's data; alerts prefixed
`[Travel — <name>]`), and the daily summary includes destination conditions.

## Safety mode (§15) & expert mode (§100)

- **Safety mode** (existing toggle): warnings first; critical/life-safety
  alerts bypass quiet-hour filtering; strong visual state on critical cards;
  official source shown prominently; "official guidance" notes appear **only**
  on `kind=official` alerts (never generated for analysis).
- **Expert mode** (separate toggle, Settings): shows version, confidence,
  source metadata, raw alert fields and the full revision history on each
  alert card. **Never hides critical warnings.**

## Daily summary (§98)

`GET /v1/notify/summary/daily?location_id=&user_id=` — built from real data
only: latest forecast, active official warnings, 24h quake detections (M4.5+),
tsunami status, AQI, active hazards, travel context, and a data-quality note
when sources are stale. When nothing meaningful happened it says exactly that:
*"Nothing significant in the last 24 hours."*

## API reference

| method | path | purpose |
|---|---|---|
| GET | `/v1/notify/config` | FCM config probe (honest) + channels |
| GET | `/v1/notify/channels` | channel defs + Android payloads |
| POST/DELETE | `/v1/notify/tokens…` | register / refresh / invalidate tokens |
| POST | `/v1/notify/send/{alert_id}` | send push for one alert (`?dry_run=1` builds + validates the payload without credentials, never reported as delivered) |
| POST | `/v1/notify/dispatch` | send all pending (silent-hours aware) |
| GET/POST | `/v1/notify/history…` | history, unread count, mark read |
| GET | `/v1/notify/alerts/{id}` | full alert incl. version/state |
| GET | `/v1/notify/alerts/{id}/revisions` | state-machine revision history |
| GET/PUT | `/v1/notify/preferences` | thresholds + silent hours |
| GET/PUT/DELETE | `/v1/notify/travel` | travel mode |
| GET | `/v1/notify/summary/daily` | smart home summary |

## Schema notes (coordinator: please reconcile)

Another v2 workstream's DDL (`backend/db/schema.sql` + `backend/db/migrate_v2.py`)
already defines `alert_revisions`, `notification_history`, and
`alert_preferences` with shapes that differ from this workstream's task spec:

- `alert_revisions` (theirs): `(alert_id, revision, title, message, severity,
  status, changed_by, changed_at, reason)` — a *content* revision log.
  Ours (task-specified): `(alert_id, from_state, to_state, changed_at, reason,
  actor)` — a *state-machine* transition log.
  → `state_machine.py` uses `alert_revisions` when it has our columns,
  otherwise falls back to `alert_state_revisions` (adaptive, additive).
- `alert_preferences` (theirs): per-device notification-routing prefs.
  Ours: per-location thresholds + silent hours → named **`alert_thresholds`**.
- `notification_history` (theirs): reused as-is; we added `read_at` via ALTER
  guard for read/unread. Statuses used: `queued|sent|failed|skipped`
  (`skipped` = not configured / suppressed / no tokens, with reason in `error`).
- FCM tokens live in their `devices` table (`push_token`).

## Honest limitations

- FCM needs a Firebase service-account JSON + `firebase-admin` + a real device;
  without them the API returns 501 with the exact missing piece — nothing is
  ever reported delivered unless FCM accepted it.
- `delivered` means "accepted by FCM", not "seen by the user".
- Threshold/travel evaluation depends on collected data freshness; the daily
  summary says when sources are stale instead of inventing figures.
- No earthquake prediction anywhere: quake alerts are detections of past
  events (USGS), and the payload text says so.
