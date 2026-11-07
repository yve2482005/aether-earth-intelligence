# Earthquake System

## Pipeline

```
USGS GeoJSON feeds · regional networks
        │  backend/collectors/earthquake/usgs.py, regional_networks.py
        ▼
event_normalizer.py → event_deduplicator.py → event_tracker.py
        ▼
earthquake_events (canonical) · earthquake_updates (revision history)
        │  aftershock.py · shakemap.py · felt_reports.py
        ▼
/v1/earthquakes · /v1/earthquakes/{id} · alerts (kind=official/verified_observation/system_analysis)
```

## Detection, not prediction

The system **detects** earthquakes that happened. Each canonical event keeps:
origin time, latitude, longitude, depth, magnitude + magnitude type, region,
status, review status, felt count, MMI, alert level, tsunami flag, ShakeMap /
PAGER availability, source URL, and the merged list of source event ids.
Updates arrive as revisions in `earthquake_updates`; the canonical row
reflects the latest reviewed values.

Nothing in this pipeline outputs a future time, place, or magnitude. There
is no "prediction" module and the schema has no column for one.

## Dedup

Two detections merge into one canonical event when **all** hold:

- same source, or overlapping `source_event_ids`
- origin times within 120 s
- epicenters within 0.5° (great-circle approximation)
- magnitudes within 0.5

The canonical event keeps the earliest origin time and the union of source
ids. The rule is pinned by `tests/test_dedup.py` so the backend
implementation (`event_deduplicator.py`) must match it.

## Tracking and products

- `event_tracker.py`: follows an event through revisions (magnitude updates,
  review status changes).
- `shakemap.py`: links USGS ShakeMap products when available.
- `felt_reports.py`: links "Did You Feel It" response data when available.
- `aftershock.py`: aftershock **probability outlooks** from official or
  statistical sources only — displayed as "probability forecast", e.g.
  "30% chance of M5+ in the next 7 days (USGS aftershock forecast)". A
  probability is not a promise about any specific hour.

## Alerts

Earthquake alerts carry `kind` = `official` (agency bulletin),
`verified_observation` (confirmed sensor data), or `system_analysis`
(our processing). Priority 1 is official life-safety; it always outranks
lower-confidence analysis for the same event (`tests/test_alerts.py`).
`dedup_key` UNIQUE prevents duplicate alerts for the same message.

## Honest limitations

- Detection latency and completeness depend on the upstream networks; the
  system reports what the networks report, with their timestamps.
- Magnitude and location get revised; early values are preliminary and are
  labeled as such until review status says otherwise.
- Aftershock probabilities are statistical. A "low" probability does not
  mean "safe", and the UI must not imply it does.
- Regional network coverage is uneven; some areas have sparser detection.
- As of 2026-10-05 the collectors had landed but not yet run against live
  endpoints; sample outputs for `docs/verification/` are still pending.
