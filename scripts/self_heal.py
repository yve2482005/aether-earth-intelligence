#!/usr/bin/env python3
"""AETHER 1-minute self-heal watchdog (added 2026-11-06, user order).

Runs once a minute from cron `aether-watchdog-1min`. Checks, in order:
  1. aether-api service + HTTP overview (fix ONLY on inactive/dead or
     2 consecutive failures — a single slow first response is NOT a failure)
  2. lock_watchdog.py alive (relaunch if dead)
  3. collector flock held by a dead/wedged holder (kill ONLY a verified
     wedged holder; NEVER a train_cycle.py worker consuming CPU)
  4. Yangon weather freshness (one bounded collect run when stale, unless
     the flock is held or the Open-Meteo daily limit is exhausted — 429)
  5. un-alerted M5.0+ quakes in the last 6h (run the alert engine's build
     step; M5.0-5.9 are below the engine's M6.0 threshold by design)
  6. disk free space warning (<5 GiB)

SAFETY LIMITS (hard): never delete data or files; never touch a
train_cycle.py process while it consumes CPU; never call the Open-Meteo
API directly for probing (quota); never restart the VM; never push to
GitHub; never modify other crons.

Every run writes data/self_heal_status.json (atomic replace) and appends
one JSON line to data/self_heal.log. Typical healthy run: a few seconds.
Stdlib only (project backend imports are lazy, alert-build path only).
"""
from __future__ import annotations

import fcntl
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DB = ROOT / "data" / "aether.db"
DB_URI = f"file:{DB}?mode=ro"
LOCK_FILE = ROOT / "data" / ".collect_advanced.lock"
LOCK_WATCH_LOG = ROOT / "data" / "lock_watch.log"
STATUS_FILE = ROOT / "data" / "self_heal_status.json"
LOG_FILE = ROOT / "data" / "self_heal.log"
COLLECT_LOG = ROOT / "data" / "self_heal_collect.log"
VENV_PY = Path.home() / "workspace" / ".tooling" / "aether-venv" / "bin" / "python"
API_URL = "http://127.0.0.1:8000/v1/global/overview"
SERVICE = "aether-api"
WATCHDOG_SCRIPT = ROOT / "scripts" / "lock_watchdog.py"

STALE_HOURS = 3.0
WEDGE_MINUTES = 30
DISK_WARN_GB = 5.0
API_MAX_CONSECUTIVE = 2


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str):
    """Parse an ISO '...Z' timestamp. DB values carry fractional seconds
    (e.g. '2026-11-06T10:01:11.374276Z'); strptime('%S') rejects those,
    which once made the freshness check report 'no forecast rows to
    judge' while a timestamp was right there. Try fractional first."""
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
    return None


# ---------------------------------------------------------------------------
# Pure decision helpers (unit-tested in tests/test_self_heal.py)
# ---------------------------------------------------------------------------

def api_should_fix(is_active: bool, http_ok: bool, consecutive_failures: int):
    """Return (fix: bool, reason: str). Fix only on inactive/dead service
    or 2 consecutive HTTP failures; a single slow/failed response is not
    a failure."""
    if not is_active:
        return True, "service not active"
    if http_ok:
        return False, "api ok"
    if consecutive_failures >= API_MAX_CONSECUTIVE:
        return True, f"{consecutive_failures} consecutive http failures"
    return False, f"http failure {consecutive_failures} of {API_MAX_CONSECUTIVE} (watching)"


def is_rate_limited_text(text) -> bool:
    """True when an error string shows Open-Meteo quota exhaustion."""
    if not text:
        return False
    t = str(text).lower()
    return ("429" in t or "daily api request limit" in t
            or "daily limit" in t or "rate limit" in t
            or "rate_limited" in t)


def staleness_decision(stale_hours, flock_held: bool, rate_limited: bool):
    """Return (action, reason) for the weather-freshness check.

    action: 'ok' | 'collect' | 'skip_flock' | 'backoff_429'"""
    if stale_hours is None:
        return "ok", "no forecast rows to judge"
    if stale_hours <= STALE_HOURS:
        return "ok", f"fresh ({stale_hours:.1f}h stale)"
    if flock_held:
        return "skip_flock", f"stale {stale_hours:.1f}h but a pass holds the flock"
    if rate_limited:
        return "backoff_429", (f"stale {stale_hours:.1f}h but daily limit "
                               "exhausted (429) — not retrying")
    return "collect", f"stale {stale_hours:.1f}h, flock free, no 429"


def wedge_decision(elapsed_s, cpu_advanced: bool, recent_activity: bool):
    """Return (kill: bool, reason: str). Kill ONLY a holder that has held
    the lock >30 min with zero progress anywhere in its process tree and
    zero recent ingestion activity."""
    if elapsed_s is None:
        return False, "holder age unknown — leaving alone"
    if elapsed_s <= WEDGE_MINUTES * 60:
        return False, f"holder age {elapsed_s / 60:.0f}m < {WEDGE_MINUTES}m"
    if cpu_advanced:
        return False, "holder tree CPU advancing — progressing"
    if recent_activity:
        return False, "recent ingestion activity — progressing"
    return True, (f"holder age {elapsed_s / 60:.0f}m, no CPU advance, "
                  "no ingestion activity — wedged")


def _is_script_process(cmd: str, needle: str) -> bool:
    """True when `cmd` is a process whose argv[0] is a python interpreter
    and argv[1] is the script named `needle`.

    A shell whose `-c` string merely mentions the script must NOT match:
    its argv[0] is the shell, not python. (That false positive made the
    overlap guard fire on every launch.) /proc cmdlines are NUL-separated;
    test fixtures pass space-joined strings — the argv[0]/argv[1] rule
    holds for both."""
    import os.path as _osp
    toks = [t for t in cmd.split("\0" if "\0" in cmd else " ") if t]
    if len(toks) < 2:
        return False
    if not _osp.basename(toks[0]).startswith("python"):
        return False
    nxt = toks[1]
    return _osp.basename(nxt) == needle or nxt.endswith("/" + needle)


def find_pids_by_cmdline(needle: str, own_pid: int, entries=None,
                         extra_exclude=()):
    """PIDs actually running a script named `needle`, excluding own_pid
    (and any extra_exclude pids, e.g. our parent shell).
    `entries` injects [(pid, cmdline)] for tests; default scans /proc
    directly (avoids pgrep self-matches)."""
    excluded = {own_pid} | set(extra_exclude)
    out = []
    if entries is None:
        entries = []
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            pid = int(name)
            if pid in excluded:
                continue
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as fh:
                    cmd = fh.read().decode(errors="replace")
            except OSError:
                continue
            entries.append((pid, cmd))
    for pid, cmd in entries:
        if pid not in excluded and _is_script_process(cmd, needle):
            out.append(pid)
    return out


# ---------------------------------------------------------------------------
# Small live helpers
# ---------------------------------------------------------------------------

def run_cmd(argv, timeout: int):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return None, "", "timeout"
    except OSError as e:
        return None, "", str(e)


def log_line(record: dict) -> None:
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def load_status() -> dict:
    try:
        return json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_status(status: dict) -> None:
    tmp = STATUS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(status, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    os.replace(tmp, STATUS_FILE)


def ro_conn():
    return sqlite3.connect(DB_URI, uri=True, timeout=45)


# --- check 1: api ----------------------------------------------------------

def check_api(prev_consecutive: int) -> dict:
    rc, out, _ = run_cmd(["systemctl", "is-active", SERVICE], timeout=10)
    is_active = (rc == 0 and out == "active")
    rc, out, _ = run_cmd(
        ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
         "--max-time", "240", API_URL], timeout=250)
    http_ok = (rc == 0 and out == "200")
    consecutive = prev_consecutive if not http_ok else 0
    if not http_ok and is_active:
        consecutive = prev_consecutive + 1
    fix, reason = api_should_fix(is_active, http_ok, consecutive)
    result = {"status": "ok" if (is_active and http_ok) else "fail",
              "service_active": is_active, "http_code": out or None,
              "consecutive_failures": consecutive, "detail": reason,
              "fix_applied": None}
    if not fix:
        return result
    # Restore: copy unit, reload, enable+start, then re-verify with curl.
    steps = [
        ["cp", str(ROOT / "aether-api.service"),
         "/etc/systemd/system/aether-api.service"],
        ["systemctl", "daemon-reload"],
        ["systemctl", "enable", "--now", SERVICE],
    ]
    for argv in steps:
        rc, _o, err = run_cmd(argv, timeout=60)
        if rc != 0:
            result.update(status="fail",
                          fix_applied=f"restore FAILED at {' '.join(argv[:2])}: {err[:120]}")
            return result
    rc, out, _ = run_cmd(
        ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
         "--max-time", "240", API_URL], timeout=250)
    if rc == 0 and out == "200":
        result.update(status="ok", consecutive_failures=0,
                      fix_applied="aether-api restored (unit copied, daemon-reload, enable --now) + overview 200")
    else:
        result.update(status="fail",
                      fix_applied=f"restore attempted but overview still {out or 'unreachable'}")
    return result


# --- check 2: lock watchdog -------------------------------------------------

def watchdog_log_age_s():
    try:
        return time.time() - LOCK_WATCH_LOG.stat().st_mtime
    except OSError:
        return None


def check_watchdog() -> dict:
    pids = find_pids_by_cmdline("lock_watchdog.py", os.getpid())
    age = watchdog_log_age_s()
    result = {"status": "ok" if pids else "fail", "pids": pids,
              "log_age_s": None if age is None else round(age),
              "fix_applied": None}
    if pids:
        if age is not None and age > 600:
            result["detail"] = (f"alive (pid {pids[0]}) but log stale "
                                f"{age / 60:.0f}m — no event to log, no relaunch")
        else:
            result["detail"] = f"alive (pid {pids[0]})"
        return result
    # Dead: relaunch detached, verify a fresh log line appears.
    with open(os.devnull, "wb") as dn:
        subprocess.Popen([str(VENV_PY), str(WATCHDOG_SCRIPT)],
                         start_new_session=True, stdout=dn, stderr=dn,
                         cwd=str(ROOT))
    for _ in range(5):
        time.sleep(1)
        if find_pids_by_cmdline("lock_watchdog.py", os.getpid()):
            break
    pids = find_pids_by_cmdline("lock_watchdog.py", os.getpid())
    age = watchdog_log_age_s()
    if pids and age is not None and age < 120:
        result.update(status="ok", pids=pids,
                      fix_applied="lock_watchdog.py relaunched + fresh log line verified")
    else:
        result.update(status="fail", pids=pids,
                      fix_applied="lock_watchdog.py relaunch attempted but not verified alive")
    return result


# --- check 3: flock ----------------------------------------------------------

def try_flock_free() -> bool:
    """True when the collector flock can be acquired right now."""
    try:
        with open(LOCK_FILE, "a+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fh, fcntl.LOCK_UN)
        return True
    except (OSError, BlockingIOError):
        return False


def find_flock_holder():
    """PID holding the flock on the lock file (fd scan), or None."""
    try:
        target = os.path.realpath(LOCK_FILE)
    except OSError:
        return None
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        pid = int(name)
        try:
            fds = os.listdir(f"/proc/{pid}/fd")
        except OSError:
            continue
        for fd in fds:
            try:
                if os.path.realpath(f"/proc/{pid}/fd/{fd}") == target:
                    return pid
            except OSError:
                continue
    return None


def _proc_stat_fields(pid: int):
    try:
        with open(f"/proc/{pid}/stat") as fh:
            parts = fh.read().rsplit(")", 1)[1].split()
        # parts[0]=state, [1]=ppid, [11]=utime, [12]=stime, [19]=starttime
        return {"state": parts[0], "ppid": int(parts[1]),
                "utime": int(parts[11]), "stime": int(parts[12]),
                "starttime": int(parts[19])}
    except (OSError, ValueError, IndexError):
        return None


def _all_pids():
    return [int(n) for n in os.listdir("/proc") if n.isdigit()]


def tree_cpu_ticks(pid: int) -> int:
    """utime+stime of pid + all its descendants (a healthy pass does its
    work in children; the parent alone may look idle)."""
    total, seen, stack = 0, set(), [pid]
    stats = {}
    for p in _all_pids():
        s = _proc_stat_fields(p)
        if s:
            stats[p] = s
    while stack:
        cur = stack.pop()
        if cur in seen or cur not in stats:
            continue
        seen.add(cur)
        total += stats[cur]["utime"] + stats[cur]["stime"]
        stack.extend(p for p, s in stats.items() if s["ppid"] == cur)
    return total


def proc_elapsed_s(pid: int):
    try:
        with open("/proc/stat") as fh:
            btime = next(int(l.split()[1]) for l in fh if l.startswith("btime"))
        s = _proc_stat_fields(pid)
        if not s:
            return None
        ticks = os.sysconf("SC_CLK_TCK")
        return time.time() - (btime + s["starttime"] / ticks)
    except (OSError, ValueError, StopIteration):
        return None


def proc_cmdline(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return fh.read().replace(b"\0", b" ").decode(errors="replace").strip()
    except OSError:
        return ""


def recent_ingestion_activity(conn, minutes: int = 15) -> bool:
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM ingestion_runs "
            "WHERE started_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now', ?) "
            "AND status IN ('ok','running','success')",
            (f"-{minutes} minutes",)).fetchone()
        return bool(row and row[0] > 0)
    except sqlite3.Error:
        return False


def check_flock(conn) -> dict:
    result = {"status": "ok", "held": False, "holder_pid": None,
              "holder_cmd": None, "detail": "flock free", "fix_applied": None}
    if try_flock_free():
        return result
    holder = find_flock_holder()
    result.update(held=True, holder_pid=holder)
    if holder is None:
        result.update(status="fail",
                      detail="flock held but holder PID not found — leaving alone")
        return result
    cmd = proc_cmdline(holder)
    result["holder_cmd"] = cmd[:120]
    elapsed = proc_elapsed_s(holder)
    before = tree_cpu_ticks(holder)
    time.sleep(5)
    after = tree_cpu_ticks(holder)
    cpu_advanced = after > before
    activity = recent_ingestion_activity(conn)
    kill, reason = wedge_decision(elapsed, cpu_advanced, activity)
    result["detail"] = f"holder pid={holder} ({cmd[:60]}): {reason}"
    if not kill:
        return result
    # Verify it is a collector/pass process before killing; never kill a
    # train worker that is consuming CPU (wedge_decision already required
    # no CPU advance, and this is a second guard).
    if "train_cycle" in cmd and cpu_advanced:
        result.update(status="fail",
                      detail="holder is a live train worker — refusing to kill")
        return result
    if not any(k in cmd for k in ("run_collect_advanced", "run_system",
                                  "train_cycle", "run_collect.py")):
        result.update(status="fail",
                      detail=f"holder cmdline not a known collector/pass process: {cmd[:80]}")
        return result
    try:
        os.kill(holder, signal.SIGTERM)
    except OSError as e:
        result.update(status="fail", detail=f"SIGTERM to {holder} failed: {e}")
        return result
    for _ in range(10):
        time.sleep(1)
        if try_flock_free():
            result.update(status="ok",
                          fix_applied=f"wedged flock holder pid={holder} SIGTERMed; flock freed")
            return result
    result.update(status="fail",
                  fix_applied=f"SIGTERM sent to wedged holder pid={holder} but flock still held")
    return result


# --- check 4: weather freshness ---------------------------------------------

def yangon_freshness(conn):
    try:
        row = conn.execute(
            "SELECT collected_at FROM weather_forecasts WHERE location_id=1 "
            "ORDER BY collected_at DESC LIMIT 1").fetchone()
    except sqlite3.Error:
        return None, None
    if not row or not row[0]:
        return None, None
    ts = parse_iso(row[0])
    if ts is None:
        return row[0], None
    return row[0], (utc_now() - ts).total_seconds() / 3600.0


def rate_limited(conn):
    """True when stored state shows Open-Meteo quota exhaustion (429).
    Sources: bulk-sweep state last_error + recent ingestion_runs errors.
    Never probes the API directly (quota)."""
    sources = []
    try:
        row = conn.execute(
            "SELECT value FROM global_collect_state WHERE key='sweep'").fetchone()
        if row:
            last_error = json.loads(row[0]).get("last_error") or ""
            if is_rate_limited_text(last_error):
                sources.append(f"sweep last_error={last_error[:80]}")
    except (sqlite3.Error, ValueError):
        pass
    try:
        rows = conn.execute(
            "SELECT error FROM ingestion_runs WHERE error IS NOT NULL "
            "AND started_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now', '-24 hours') "
            "ORDER BY started_at DESC LIMIT 20").fetchall()
        for (err,) in rows:
            if is_rate_limited_text(err):
                sources.append(f"ingestion_runs error={err[:80]}")
                break
    except sqlite3.Error:
        pass
    return bool(sources), "; ".join(sources)


def check_weather(conn, flock_held: bool) -> dict:
    collected_at, stale_hours = yangon_freshness(conn)
    limited, limited_src = rate_limited(conn)
    action, reason = staleness_decision(stale_hours, flock_held, limited)
    result = {"status": "ok", "yangon_max_collected_at": collected_at,
              "stale_hours": None if stale_hours is None else round(stale_hours, 2),
              "rate_limited": limited, "rate_limit_source": limited_src or None,
              "action": action, "detail": reason, "fix_applied": None}
    if action != "collect":
        if action in ("skip_flock", "backoff_429"):
            result["status"] = "warn"
        return result
    # One bounded collect, detached so this run stays fast; the flock
    # guard makes it a no-op if another run grabs the lock first.
    with open(COLLECT_LOG, "ab") as lf:
        proc = subprocess.Popen(
            ["timeout", "600", str(VENV_PY),
             str(ROOT / "scripts" / "run_collect_advanced.py")],
            stdout=lf, stderr=subprocess.STDOUT,
            start_new_session=True, cwd=str(ROOT))
    result.update(status="ok",
                  fix_applied=f"bounded collect triggered in background (pid {proc.pid}, 600s cap)")
    return result


# --- check 5: un-alerted M5.0+ ----------------------------------------------

def unalerted_quakes(conn):
    try:
        rows = conn.execute(
            """SELECT e.canonical_event_id, e.magnitude, e.origin_time, e.region
               FROM earthquake_events e
               LEFT JOIN alerts a ON a.event_id = e.canonical_event_id
                 AND a.type = 'EARTHQUAKE'
               WHERE e.magnitude >= 5.0
                 AND e.origin_time >= strftime('%Y-%m-%dT%H:%M:%SZ','now', '-6 hours')
                 AND a.alert_id IS NULL
               ORDER BY e.origin_time DESC""").fetchall()
        return [dict(zip(("id", "magnitude", "origin_time", "region"), r))
                for r in rows]
    except sqlite3.Error:
        return []


def run_alert_build():
    """Run the alert engine's build step (idempotent, dedup-safe)."""
    try:
        from backend.alerting.engine import build_alerts  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001
        return None, f"alert engine import failed: {e}"
    try:
        conn = sqlite3.connect(str(DB), timeout=30)
        # The engine does dict(row) everywhere: it needs Row objects,
        # not plain tuples (a plain connection breaks alerts_from_tsunami
        # with ValueError while the other builders still run).
        conn.row_factory = sqlite3.Row
        try:
            return build_alerts(conn), None
        finally:
            conn.close()
    except sqlite3.Error as e:
        return None, f"alert build DB error: {e}"


def check_alerts(conn) -> dict:
    missing = unalerted_quakes(conn)
    result = {"status": "ok", "unalerted_m5": len(missing),
              "events": missing, "fix_applied": None}
    if not missing:
        result["detail"] = "no un-alerted M5.0+ in last 6h"
        return result
    big = [m for m in missing if (m["magnitude"] or 0) >= 6.0]
    small = [m for m in missing if (m["magnitude"] or 0) < 6.0]
    if not big:
        result.update(status="warn",
                      detail=(f"{len(small)} M5.0-5.9 without engine alerts — "
                              "below the engine's M6.0 threshold by design; "
                              "chat delivery is a separate path"))
        return result
    counts, err = run_alert_build()
    if err:
        result.update(status="fail", detail=f"alert build failed: {err}")
        return result
    result.update(status="ok",
                  fix_applied=f"alert engine build ran for {len(big)} M6.0+ event(s): {counts}")
    return result


# --- check 6: disk -----------------------------------------------------------

def check_disk() -> dict:
    try:
        free_gb = shutil.disk_usage(ROOT / "data").free / (2 ** 30)
    except OSError:
        return {"status": "fail", "free_gb": None, "detail": "disk_usage failed"}
    ok = free_gb >= DISK_WARN_GB
    return {"status": "ok" if ok else "warn", "free_gb": round(free_gb, 2),
            "detail": (f"{free_gb:.1f} GiB free"
                       + ("" if ok else f" — below {DISK_WARN_GB} GiB warning line"))}


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def already_running() -> bool:
    me = os.getpid()
    return bool(find_pids_by_cmdline("self_heal.py", me,
                                     extra_exclude=(os.getppid(),)))


def main() -> int:
    started = utc_now()
    if already_running():
        log_line({"run_at": iso(started), "skipped": "overlapping run"})
        return 0
    prev = load_status()
    prev_consec = 0
    try:
        prev_consec = int((prev.get("checks") or {}).get("api", {}).get("consecutive_failures") or 0)
    except (TypeError, ValueError):
        prev_consec = 0

    checks, fixes, attention = {}, [], []
    try:
        checks["api"] = check_api(prev_consec)
        checks["watchdog"] = check_watchdog()
        with ro_conn() as conn:
            checks["flock"] = check_flock(conn)
            checks["weather"] = check_weather(conn, checks["flock"]["held"])
            checks["alerts"] = check_alerts(conn)
        checks["disk"] = check_disk()
    except Exception as e:  # noqa: BLE001 - one bad check must not kill the run
        checks.setdefault("error", {"status": "fail", "detail": f"unexpected: {e}"})

    for name, c in checks.items():
        if isinstance(c, dict) and c.get("fix_applied"):
            fixes.append(f"{name}: {c['fix_applied']}")
        if isinstance(c, dict) and c.get("status") in ("fail", "warn"):
            attention.append(f"{name}: {c.get('detail') or c.get('status')}")

    status = {
        "last_run_utc": iso(started),
        "duration_s": round((utc_now() - started).total_seconds(), 1),
        "checks": checks,
        "fixes_applied": fixes,
        "needs_attention": attention,
    }
    try:
        save_status(status)
    except OSError as e:
        print(f"self_heal: cannot write status: {e}", file=sys.stderr)
        return 1
    log_line({"run_at": iso(started),
              "duration_s": status["duration_s"],
              "checks": {k: v.get("status") for k, v in checks.items()
                         if isinstance(v, dict)},
              "fixes": fixes, "attention": attention})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
