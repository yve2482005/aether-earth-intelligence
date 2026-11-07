"""Tests for the 1-minute self-heal watchdog (scripts/self_heal.py).

Fixtures only — no live services, no API calls, no real flock file, no
real DB. Pure decision functions are tested directly; the flock helpers
are tested against a temp file with the module's LOCK_FILE monkeypatched.
"""
import fcntl
import importlib.util
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_SPEC = importlib.util.spec_from_file_location(
    "self_heal",
    os.path.join(os.path.dirname(__file__), "..", "scripts", "self_heal.py"))
sh = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sh)


# --- check 1: api fix decision -------------------------------------------

def test_api_ok_no_fix():
    fix, _ = sh.api_should_fix(True, True, 0)
    assert fix is False


def test_api_inactive_fixes_immediately():
    fix, reason = sh.api_should_fix(False, False, 0)
    assert fix is True
    assert "not active" in reason


def test_api_inactive_even_if_http_ok():
    # systemctl says dead: fix even if a stray curl happened to answer.
    fix, _ = sh.api_should_fix(False, True, 0)
    assert fix is True


def test_api_first_http_failure_only_watches():
    fix, reason = sh.api_should_fix(True, False, 0)
    assert fix is False
    assert "watching" in reason
    fix, _ = sh.api_should_fix(True, False, 1)
    assert fix is False


def test_api_second_consecutive_failure_fixes():
    fix, reason = sh.api_should_fix(True, False, 2)
    assert fix is True
    assert "2 consecutive" in reason
    fix, _ = sh.api_should_fix(True, False, 3)
    assert fix is True


# --- 429 detection ---------------------------------------------------------

def test_rate_limited_strings():
    assert sh.is_rate_limited_text("HTTP 429")
    assert sh.is_rate_limited_text("Daily API request limit exceeded. Please try again tomorrow.")
    assert sh.is_rate_limited_text("error: rate limit hit")
    assert sh.is_rate_limited_text("stopped_429")
    assert sh.is_rate_limited_text("Rate_Limited by provider")


def test_not_rate_limited_strings():
    assert sh.is_rate_limited_text("connection timeout") is False
    assert sh.is_rate_limited_text("database is locked") is False
    assert sh.is_rate_limited_text("") is False
    assert sh.is_rate_limited_text(None) is False


# --- staleness decision table ----------------------------------------------

def test_staleness_fresh_is_ok():
    action, _ = sh.staleness_decision(1.0, False, False)
    assert action == "ok"
    action, _ = sh.staleness_decision(3.0, False, False)
    assert action == "ok"  # boundary: <= 3h is fresh


def test_staleness_no_rows_is_ok():
    action, _ = sh.staleness_decision(None, False, False)
    assert action == "ok"


def test_staleness_429_means_backoff_not_retry():
    action, reason = sh.staleness_decision(5.0, False, True)
    assert action == "backoff_429"
    assert "429" in reason


def test_staleness_flock_held_means_skip():
    action, _ = sh.staleness_decision(5.0, True, False)
    assert action == "skip_flock"
    # flock takes precedence over the 429 branch (a pass is collecting)
    action, _ = sh.staleness_decision(5.0, True, True)
    assert action == "skip_flock"


def test_staleness_free_and_clean_collects():
    action, _ = sh.staleness_decision(5.0, False, False)
    assert action == "collect"


# --- wedge decision ----------------------------------------------------------

def test_wedge_young_holder_left_alone():
    kill, _ = sh.wedge_decision(100, False, False)
    assert kill is False
    kill, _ = sh.wedge_decision(30 * 60, False, False)
    assert kill is False  # boundary: <= 30 min


def test_wedge_cpu_advancing_is_progress():
    kill, reason = sh.wedge_decision(3600, True, False)
    assert kill is False
    assert "CPU" in reason


def test_wedge_recent_ingestion_is_progress():
    kill, _ = sh.wedge_decision(3600, False, True)
    assert kill is False


def test_wedge_old_idle_no_activity_kills():
    kill, reason = sh.wedge_decision(3600, False, False)
    assert kill is True
    assert "wedged" in reason


def test_wedge_unknown_age_never_kills():
    kill, _ = sh.wedge_decision(None, False, False)
    assert kill is False


# --- pid scan (injected entries, no /proc reads) ----------------------------

def test_find_pids_by_cmdline_matches_and_excludes_self():
    entries = [(10, "python scripts/lock_watchdog.py"),
               (11, "python scripts/run_system.py"),
               (12, "python scripts/lock_watchdog.py"),
               (99, "python scripts/lock_watchdog.py")]
    assert sh.find_pids_by_cmdline("lock_watchdog.py", 99, entries) == [10, 12]
    assert sh.find_pids_by_cmdline("run_system.py", 99, entries) == [11]
    assert sh.find_pids_by_cmdline("self_heal.py", 99, entries) == []


def test_find_pids_ignores_shell_that_mentions_script():
    # Regression: the invoking shell's `-c` string contains the script
    # name mid-command; it must NOT count as a running instance.
    entries = [(50, "bash -c cd /x && /venv/bin/python scripts/self_heal.py; echo done"),
               (51, "python scripts/self_heal.py")]
    assert sh.find_pids_by_cmdline("self_heal.py", 99, entries) == [51]


# --- flock helpers against a temp file ---------------------------------------

def test_flock_free_then_held(tmp_path, monkeypatch):
    lock = tmp_path / "test.lock"
    lock.write_text("")
    monkeypatch.setattr(sh, "LOCK_FILE", lock)
    assert sh.try_flock_free() is True
    fh = open(lock, "a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert sh.try_flock_free() is False
        # holder detection finds this very process
        assert sh.find_flock_holder() == os.getpid()
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()
    assert sh.try_flock_free() is True
    assert sh.find_flock_holder() is None


# --- status json round-trip ---------------------------------------------------

def test_parse_iso_fractional_seconds():
    # Regression: DB timestamps carry fractional seconds; the first live
    # run parsed '...10:01:11.374276Z' as None and reported "no forecast
    # rows to judge" while a row was right there.
    dt = sh.parse_iso("2026-11-06T10:01:11.374276Z")
    assert dt is not None and dt.hour == 10 and dt.minute == 1
    assert dt.second == 11 and dt.microsecond == 374276
    dt2 = sh.parse_iso("2026-11-06T10:01:11Z")
    assert dt2 is not None and dt2.second == 11
    assert sh.parse_iso("not-a-date") is None
    assert sh.parse_iso(None) is None


def test_status_round_trip(tmp_path, monkeypatch):
    status_file = tmp_path / "self_heal_status.json"
    monkeypatch.setattr(sh, "STATUS_FILE", status_file)
    payload = {"last_run_utc": "2026-11-06T16:00:00Z",
               "checks": {"api": {"status": "ok", "consecutive_failures": 1}}}
    sh.save_status(payload)
    assert sh.load_status() == payload
    assert json.loads(status_file.read_text())["checks"]["api"]["status"] == "ok"


def test_load_status_missing_file_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(sh, "STATUS_FILE", tmp_path / "nope.json")
    assert sh.load_status() == {}
