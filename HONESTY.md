# AETHER Earth Intelligence — Scientific Honesty Rules

This project reports on a physical planet that it cannot control and mostly
cannot predict. These rules are hard requirements, not guidelines. They are
enforced by `tests/test_honesty.py`, which fails the build if forbidden
certainty phrases appear in user-facing strings.

## The six distinct labels

Every datum shown to a user must carry one of these kinds, and the kinds are
never mixed:

| Label | Meaning | Example |
|---|---|---|
| **OBSERVATION** | Something a sensor or human directly measured | "Yangon station measured 34.2 C at 14:00 UTC" |
| **DETECTION** | An instrument network picked up an event that happened | "USGS detected a magnitude 5.1 earthquake" |
| **FORECAST** | A model estimate of the near future, with uncertainty | "Model estimate: 40% chance of rain at 15:00" |
| **WARNING** | An official message from an authoritative agency | "NWS issued a tsunami warning for ..." |
| **RISK** | A historical/statistical estimate of long-run hazard | "Historical seismic hazard in this region is high" |
| **PREDICTION** | A claim about a specific future event's time/place/size | **Never produced for earthquakes** |

FORECAST is an estimate with error bars. WARNING is someone else's official
message, relayed verbatim with attribution. RISK describes long-term hazard,
not tomorrow's events. PREDICTION of earthquakes is not something science can
do reliably, and AETHER does not pretend otherwise.

## Earthquakes: no prediction, ever

- AETHER **detects** earthquakes from USGS and regional networks. Detection is
  honest: event time, location, magnitude, depth, as reported, with source and
  update timestamps.
- AETHER does **not** produce earthquake predictions. It will never say an
  earthquake is coming at a given time, place, or magnitude.
- Aftershock outlooks are **probabilities** from official or statistical
  sources only, labeled "probability forecast, not a guarantee of safety or
  occurrence." A probability of 30% means it happens 3 times out of 10 in
  similar situations — it says nothing about this specific hour.
- The ML risk module (`ml/risk/seismic_risk.py`) computes **historical and
  hazard statistics** (past event rates, hazard curves). It does not output
  "the next earthquake".

## Tsunamis: official messages only

- Tsunami information comes **only from official warning messages**
  (e.g. NWS CAP tsunami products). The parser preserves the message's
  original classification (`raw_classification`) and shows it alongside the
  parsed fields.
- When correlating an earthquake with tsunami potential, the system may say
  **"potentially relevant earthquake detected"**. It must **never** say a
  tsunami is coming unless an authoritative agency has issued a warning.
- Official warning categories — WARNING / ADVISORY / WATCH / INFORMATION /
  NO THREAT — are shown with their original wording plus a localized
  explanation. The category wording itself is never paraphrased in a way that
  changes its meaning.

## Metrics honesty

- Weather model skill is reported only as standard per-task metrics:
  MAE, RMSE, Brier score, CRPS, calibration error — with sample counts and
  evaluation periods. No single "accuracy %" number is presented as the
  quality of the system.
- An unavailable metric is shown as unavailable (N/A with a reason), never as
  zero and never as a made-up number.
- Simulation results (e.g. JLPT-style readiness analogs) are labeled
  simulation-only.

## Unavailable data is stated, not hidden

When a source is down or a datum is missing, the interface shows an honest
unavailable state with the timestamp of the last verified update, e.g.
"Official tsunami data currently unavailable. Last verified: 2026-10-05
14:20 UTC." Satellite and radar layers report availability **per source**.

## What the app never says

These phrasings are banned from all user-facing strings (API messages and
UI). Each is stated here only as a ban, never as a claim the app makes:

- never claim perfect accuracy ("100% accurate" and similar)
- never promise a certain outcome ("guaranteed" and similar)
- never phrase anything as an earthquake prediction ("earthquake will ..."
  and similar)
- never assert a tsunami is coming without an authoritative warning behind
  it ("tsunami will occur" and similar)

The build enforces this. If a banned phrasing is found outside a ban
statement like the ones above, the test suite fails and the release does
not ship.

## v2 platform honesty (workstream E)

These describe the API/DB platform, not the planet — but the same honesty
standard applies:

- **Auth is disabled by default.** Until `AETHER_API_KEY` is set, the API
  accepts every request. Operators must not assume the API is private.
- **Rate limiting is in-memory and per-process** (token bucket, 120
  req/min/IP on `/v1/*`). A process restart clears the buckets. It is sized
  for fair use on a single host, not as protection against a determined
  flood — and it has not been load-tested against adversarial traffic.
- **Log format**: plain text by default; JSON only with
  `AETHER_LOG_JSON=1`. Nothing is logged that could identify a user beyond
  the client IP already visible to any web server.
- **`prune.py` is dry-run by default.** Retention deletes raw rows only
  per the policy in `docs/OPERATIONS.md`; the scientific record
  (earthquakes, tsunamis, alerts, model registry) is never auto-pruned,
  and pruning is never presented as reversible — deleted rows are gone
  unless a backup holds them.
- **Envelope metadata is constructed at response time.** `retrieved_at` /
  `processed_at` default to "now" when a route does not supply measured
  values; they describe the response, not the planet.
