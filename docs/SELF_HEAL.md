# AETHER Self-Heal Watchdog (1-minute)

Added 2026-11-06 on the user's order: *"တမိနစ်တခါ စောင့်ကြည့်ပီးမှန်မမှန်စစ်ဆေးပေးမဲ့အပြင်တခါတည်းပြင်ပေးတဲ့စနစ်"* — a system that watches once a minute, checks whether things are correct, and fixes problems immediately on its own.

- Script: `scripts/self_heal.py` (stdlib only; project venv python)
- Cron: `aether-watchdog-1min` (every 1 min, goal-owned)
- State: `data/self_heal_status.json` (rewritten every run)
- Log: `data/self_heal.log` (one JSON line per run)
- Tests: `tests/test_self_heal.py` (22 tests, fixtures only)

## Checks (in order, every run)

### 1. aether-api service
`systemctl is-active aether-api` + `curl` GET `/v1/global/overview`
(240 s timeout — the first request after a startup can take 60–90 s, and
under heavy DB contention (suite/build/sheets running) a live overview took
~180 s on 2026-11-06, so a single slow response is NOT a failure).
**Fix:** only when the service is inactive/dead, or after **2 consecutive**
HTTP failures. Restore = copy `aether-api.service` to /etc, `daemon-reload`,
`systemctl enable --now aether-api`, then re-verify with curl (200).
Consecutive-failure count persists in `self_heal_status.json`.

### 2. lock_watchdog.py alive
Process scan by exact argv (`python` + `lock_watchdog.py` as argv[1] —
never a bare `pgrep` substring, which self-matches). Also records
`data/lock_watch.log` age; a stale log with a live process is only a note
(the watchdog logs only on lock events), never a relaunch trigger.
**Fix:** relaunch detached (`setsid nohup … >/dev/null 2>&1`) and verify
a fresh log line appears.

### 3. Collector flock
Non-blocking `flock` attempt on `data/.collect_advanced.lock`. When held,
the holder PID is found by scanning `/proc/*/fd`. Progress is judged by
**the whole process tree's CPU time** (a healthy pass works in children)
plus recent `ingestion_runs` activity, sampled 5 s apart.
**Fix:** kill ONLY when the holder is wedged — holds the lock **>30 min**
with **zero** CPU advance in its tree **and zero** recent ingestion
activity — and its cmdline is a known collector/pass process. SIGTERM,
then verify the flock frees within 10 s. **Never** kill a `train_cycle.py`
worker while it consumes CPU (a live train is progress, not a wedge).
If the holder cannot be identified or the flock does not free, it is
logged as needing attention — never escalated blindly.

### 4. Yangon weather freshness
One lightweight read-only query: latest `collected_at` for location 1.
**Fix:** when >3 h stale, the flock is free, and the daily limit is NOT
exhausted, trigger **one** bounded collect run (`timeout 600`,
`run_collect_advanced.py`) detached in the background, so this run stays
fast. **Back-off:** when stored state shows Open-Meteo quota exhaustion
(bulk-sweep `last_error` = HTTP 429, or recent `ingestion_runs` 429
errors), do NOT retry and never probe the API directly (quota). When a
pass holds the flock, skip — the pass is collecting.

### 5. Un-alerted M5.0+ quakes (last 6 h)
`earthquake_events` M≥5.0 with no matching `EARTHQUAKE` alert row.
**Fix:** run the alert engine's build step (`backend.alerting.engine.
build_alerts`, idempotent, dedup-safe). M5.0–5.9 events have no engine
alert **by design** (engine threshold is M6.0); those are logged, never
fabricated into alerts. Chat delivery (M5.0+) is a separate path.

### 6. Disk free space
Warn when free space on the data volume is **<5 GiB**. Never deletes
anything automatically.

## Safety limits (hard, never overridden by this script)
- Never delete data or files.
- Never touch a `train_cycle.py` process while it consumes CPU.
- Never call the Open-Meteo API directly for probing (quota).
- Never restart the VM. Never push to GitHub. Never modify other crons.
- Overlapping runs exit quietly (a previous `self_heal.py` instance is
  detected by exact argv match).

## Reporting
The cron reports to the user **only** when a fix was applied or a new
failure class appeared that the watchdog cannot fix. Healthy runs are
silent. `data/self_heal_status.json` always shows the last run:
per-check status, fixes applied, and `needs_attention` items.
