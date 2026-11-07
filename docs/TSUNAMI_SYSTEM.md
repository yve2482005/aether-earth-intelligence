# Tsunami System

## The one rule

**Only official warning messages are ingested and shown.** The parser keeps
the message's original classification in `raw_classification` and never
rewrites it. If no official message exists, there is no tsunami content —
only an honest "no official data" state.

## Pipeline

```
NWS CAP tsunami products (api.weather.gov, 10-min poll; User-Agent required)
        │  backend/collectors/tsunami/warning_sources.py, message_parser.py
        ▼
tsunami_events (message_type = ORIGINAL classification preserved)
tsunami_messages (raw_text kept verbatim)
        │  event_matcher.py (link to earthquake_events) · arrival.py · coastal_risk.py
        ▼
/v1/tsunami · alerts (kind=official, priority 1 for WARNING)
```

## Message handling

- `message_parser.py` extracts severity, affected region, issue/valid/arrival
  times, status (active/expired/cancelled/superseded) — and stores
  `raw_classification` untouched.
- `event_matcher.py` may link a tsunami message to a detected earthquake.
  The linkage language is fixed: **"potentially relevant earthquake
  detected"**. It never asserts that a tsunami is occurring or is about to
  occur.
- Superseded/cancelled/expired messages update `status`; the UI shows the
  current status with the message history, so a cancelled warning is visibly
  cancelled, not silently removed.

## Categories are never reworded

Official categories — WARNING, ADVISORY, WATCH, INFORMATION, NO THREAT —
are displayed with their original wording plus a localized explanation side
by side (Burmese-first per the i18n contract). The category wording itself
is never paraphrased into something stronger or weaker.

## Arrival and coastal risk

`arrival.py` computes estimated arrival times **only from official message
content**; `coastal_risk.py` marks coastal locations (`locations.is_coastal`)
for relevance filtering. Neither module invents a threat level the official
message did not state.

## Alerts

Tsunami alerts are `kind=official`. A WARNING maps to priority 1 /
severity `warning` or `critical`. `dedup_key` (e.g. `tsunami:ptwc:msg123`)
is UNIQUE so re-polls and re-issues do not duplicate alerts.

## Honest limitations

- If the official feed is unreachable, the endpoint returns an honest
  unavailable state with the last verified timestamp. There is no backup
  source and none is invented.
- Coverage follows the issuing agencies' areas of responsibility; outside
  them the honest answer is "no official data for this region".
- Arrival estimates are only as good as the official message; the app shows
  them as estimates with the source attached.
- As of 2026-10-05 the collectors had landed but not yet run against live
  endpoints; NWS CAP terms require re-verification before production use
  (see DATA_LICENSES.md).
