#!/usr/bin/env python3
"""Lock watchdog (added 2026-10-18): samples the live DB write lock every
10s. When BEGIN IMMEDIATE cannot be acquired within 1s, it snapshots every
process holding the DB/WAL open (cmdline + state + current syscall + IO
counters) into data/lock_watch.log, and logs again when the lock frees —
so the process whose exit/activity brackets a lock storm is identifiable.
Read-only toward the DB (BEGIN IMMEDIATE is rolled back immediately).
"""
import os
import signal
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "aether.db"
LOG = ROOT / "data" / "lock_watch.log"
SAMPLE_S = 10


def log(line: str) -> None:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {line}\n")
    except OSError:
        pass


def lock_free() -> bool:
    try:
        conn = sqlite3.connect(str(DB), timeout=1)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("ROLLBACK")
            return True
        finally:
            conn.close()
    except sqlite3.OperationalError:
        return False


def passive_checkpoint() -> None:
    """Dedicated WAL checkpointer (2026-10-18): production connections
    no longer autocheckpoint inline (a checkpoint inside a commit holds
    the write lock for its whole copy — a core lock-storm mechanism).
    This runs wal_checkpoint(PASSIVE) from the watchdog's own short
    connection: it copies what it can without blocking writers and
    never holds the write lock. Logged only when it cannot finish
    (busy>0), which is itself diagnostic."""
    try:
        conn = sqlite3.connect(str(DB), timeout=5)
        try:
            row = conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        finally:
            conn.close()
        if row and row[0]:
            log(f"  checkpoint busy: busy={row[0]} wal_pages={row[1]} "
                f"checkpointed={row[2]}")
    except sqlite3.Error:
        pass


def _is_main_db(link: str) -> bool:
    base = link.split(" (deleted)")[0].rstrip("/").rsplit("/", 1)[-1]
    return base in ("aether.db", "aether.db-wal", "aether.db-shm")


def db_holders() -> list:
    out = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        fd_dir = f"/proc/{pid}/fd"
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        holds = False
        for fd in fds:
            try:
                if _is_main_db(os.readlink(os.path.join(fd_dir, fd))):
                    holds = True
                    break
            except OSError:
                continue
        if not holds:
            continue
        # Per-field best-effort: /proc/<pid>/io and /syscall can be
        # ptrace-restricted in this sandbox; a failed read must never
        # drop the holder itself (that bug hid every holder in the
        # first watchdog version).
        cmd = state = syscall = write_bytes = "?"
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                cmd = fh.read().replace(b"\0", b" ").decode(errors="replace").strip()
        except OSError:
            pass
        try:
            with open(f"/proc/{pid}/status") as fh:
                state = next((l.split(":", 1)[1].strip() for l in fh
                              if l.startswith("State:")), "?")
        except OSError:
            pass
        try:
            with open(f"/proc/{pid}/syscall") as fh:
                syscall = fh.read().strip().split(" ")[0]
        except OSError:
            pass
        try:
            with open(f"/proc/{pid}/io") as fh:
                io = dict(l.split(": ") for l in fh.read().splitlines() if ": " in l)
            write_bytes = io.get("write_bytes", "?")
        except OSError:
            pass
        # Per-thread states: the thread actually executing a write is
        # R/D while lock waiters sleep in the busy handler (S). This
        # distinguishes the lock HOLDER from fellow victims when every
        # stack shows the same sqlite call.
        threads = "?"
        try:
            tstates = []
            for tid in sorted(os.listdir(f"/proc/{pid}/task")):
                try:
                    with open(f"/proc/{pid}/task/{tid}/stat") as fh:
                        tstates.append(fh.read().rsplit(")", 1)[1].split()[0])
                except OSError:
                    continue
            threads = "".join(tstates)
        except OSError:
            pass
        out.append(f"pid={pid} state={state} threads={threads} "
                   f"syscall={syscall} "
                   f"write_bytes={write_bytes} cmd={cmd[:110]}")
    return out


def kernel_locks() -> list:
    """fcntl locks on the DB files from /proc/locks (kernel truth).

    The fd scan can miss a holder; /proc/locks names the pid holding
    the WAL write lock directly (POSIX ADVISORY WRITE on the -shm
    inode's lock range).
    """
    inodes = set()
    for suffix in ("", "-wal", "-shm"):
        try:
            inodes.add(str(os.stat(str(DB) + suffix).st_ino))
        except OSError:
            pass
    out = []
    try:
        with open("/proc/locks") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 6 and parts[5].split(":")[-1] in inodes:
                    out.append(line.strip())
    except OSError:
        pass
    return out


def main() -> None:
    log("watchdog start — baseline holders:")
    for line in db_holders():
        log("  " + line)
    blocked = False
    tick = 0
    while True:
        tick += 1
        if tick % 3 == 0:  # ~every 30 s at SAMPLE_S=10
            passive_checkpoint()
        free = lock_free()
        if not free and not blocked:
            blocked = True
            log("LOCK BLOCKED — holders:")
            for line in kernel_locks():
                log("  KLOCK " + line)
            for line in db_holders():
                log("  " + line)
                # The collector registers SIGUSR1 as a faulthandler
                # stack-dump (see scripts/run_collect_advanced.py);
                # trigger one dump per blocked episode so the holder's
                # code path lands in data/stack_dump.log unattended.
                if "run_collect_advanced" in line:
                    try:
                        pid = int(line.split("pid=", 1)[1].split(" ", 1)[0])
                        os.kill(pid, signal.SIGUSR1)
                        os.kill(pid, signal.SIGUSR2)
                        log(f"  SIGUSR1+SIGUSR2 sent to collector pid={pid}")
                    except (ValueError, OSError):
                        pass
        elif free and blocked:
            blocked = False
            log("LOCK FREE again — holders now:")
            for line in db_holders():
                log("  " + line)
        time.sleep(SAMPLE_S)


if __name__ == "__main__":
    main()
