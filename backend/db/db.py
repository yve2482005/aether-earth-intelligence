"""SQLite wrapper for AETHER. schema.sql is the source of truth."""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from backend.common.config import DB_PATH, SCHEMA_PATH, ensure_dirs


class _TimedConnection(sqlite3.Connection):
    """Connection that logs slow or lock-failed statements to
    data/lock_diag.log (added 2026-10-18 to pin down the recurring
    'database is locked' storms: the log records HOW LONG a write
    waited before failing — ~busy_timeout means a continuous lock
    holder; ~0s means an instant BUSY_SNAPSHOT-style failure).
    Only statements taking >= 10s or raising a lock error are logged,
    so the file stays small; logging itself never raises.
    """

    _DIAG_MIN_S = 10.0

    # Connection registry (added 2026-10-18, lock-storm forensics):
    # kernel evidence (/proc/locks) proved the write lock can be held
    # by a connection inside the collector while every thread sleeps —
    # i.e. an idle OPEN transaction. Stacks cannot show which
    # connection that is, so every connection registers itself here
    # with its transaction state and last write; dump_txn_state()
    # (triggered via SIGUSR2 in run_collect_advanced) names the holder.
    _REGISTRY: dict = {}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        import threading

        self._txn_open = False
        self._txn_open_since = None
        self._txn_reported = None
        _TimedConnection._start_sentinel()
        self._last_write = ""
        self._last_action = ""
        # sqlite3.Connection cannot be weak-referenced, so the registry
        # holds strong refs; bound it (drop the oldest) so long-lived
        # processes (the API) cannot accumulate connections forever.
        reg = _TimedConnection._REGISTRY
        if len(reg) > 512:
            for old_key in list(reg)[:256]:
                reg.pop(old_key, None)
        reg[id(self)] = {
            "conn": self,
            "created_by": threading.current_thread().name,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                        time.gmtime()),
        }

    def _note(self, sql: str) -> None:
        head = str(sql).lstrip()[:6].upper()
        if head.startswith(("INSERT", "UPDATE", "DELETE", "REPLAC")):
            if not self._txn_open:
                self._txn_open = True
                self._txn_open_since = time.time()
            self._last_write = str(sql)[:80]
        self._last_action = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                          time.gmtime())

    _SENTINEL_STARTED = False

    @classmethod
    def _start_sentinel(cls) -> None:
        """Daemon thread that logs any connection whose transaction
        stays open > 15 s (data/txn_state.log). Signal-based dumps
        starve when the main thread parks in a pool join, so this runs
        independently. Diagnostics only; never raises."""
        if cls._SENTINEL_STARTED:
            return
        cls._SENTINEL_STARTED = True
        import threading

        def _watch() -> None:
            log_path = Path(DB_PATH).parent / "txn_state.log"
            while True:
                time.sleep(5)
                try:
                    now = time.time()
                    lines = []
                    for info in list(cls._REGISTRY.values()):
                        conn = info["conn"]
                        since = getattr(conn, "_txn_open_since", None)
                        if not conn._txn_open or since is None:
                            continue
                        if now - since < 15:
                            continue
                        if getattr(conn, "_txn_reported", None) == since:
                            continue
                        conn._txn_reported = since
                        try:
                            real = conn.in_transaction
                        except Exception:  # noqa: BLE001
                            real = "?"
                        lines.append(
                            f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}"
                            f" pid={os.getpid()} LONG-TXN conn={id(conn)}"
                            f" created_by={info['created_by']}"
                            f" open_for={now - since:.0f}s"
                            f" in_transaction={real}"
                            f" last_write={conn._last_write!r}"
                            f" last_action={conn._last_action}\n")
                    if lines:
                        with open(log_path, "a") as fh:
                            fh.writelines(lines)
                except Exception:  # noqa: BLE001
                    pass

        threading.Thread(target=_watch, name="txn-sentinel",
                         daemon=True).start()

    @classmethod
    def dump_txn_state(cls, fh) -> None:
        import threading

        fh.write(f"=== txn state {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}"
                 f" pid={os.getpid()} main_thread={threading.main_thread().name}\n")
        for cid, info in list(cls._REGISTRY.items()):
            conn = info["conn"]
            try:
                real = conn.in_transaction
            except Exception:  # noqa: BLE001 - cross-thread read fallback
                real = "?"
            fh.write(f"conn={cid} created_by={info['created_by']} "
                     f"created_at={info['created_at']} "
                     f"txn_open={conn._txn_open} in_transaction={real} "
                     f"last_write={conn._last_write!r} "
                     f"last_action={conn._last_action}\n")
        fh.flush()

    def _diag(self, kind: str, sql: str, elapsed: float,
              error: Optional[str] = None) -> None:
        try:
            import threading

            from backend.common.config import DATA_DIR
            line = (f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} "
                    f"pid={os.getpid()} th={threading.current_thread().name} "
                    f"{kind} elapsed={elapsed:.1f}s "
                    f"err={error or '-'} sql={str(sql)[:80]!r}\n")
            with open(DATA_DIR / "lock_diag.log", "a", encoding="utf-8") as fh:
                fh.write(line)
        except Exception:
            pass

    def commit(self) -> None:
        # Commits were the diag's blind spot (fsync/checkpoint time is
        # not part of any statement's elapsed). Time them too.
        t0 = time.monotonic()
        try:
            super().commit()
        except sqlite3.OperationalError as e:
            elapsed = time.monotonic() - t0
            if "locked" in str(e) or elapsed >= self._DIAG_MIN_S:
                self._diag("commit", "COMMIT", elapsed, str(e))
            raise
        elapsed = time.monotonic() - t0
        if elapsed >= self._DIAG_MIN_S:
            self._diag("commit", "COMMIT", elapsed)
        self._txn_open = False
        self._txn_open_since = None

    def rollback(self) -> None:
        super().rollback()
        self._txn_open = False
        self._txn_open_since = None

    def execute(self, sql, parameters=(), /):
        self._note(sql)
        t0 = time.monotonic()
        try:
            cur = super().execute(sql, parameters)
        except sqlite3.OperationalError as e:
            elapsed = time.monotonic() - t0
            if "locked" in str(e) or elapsed >= self._DIAG_MIN_S:
                self._diag("execute", sql, elapsed, str(e))
            raise
        elapsed = time.monotonic() - t0
        if elapsed >= self._DIAG_MIN_S:
            self._diag("execute", sql, elapsed)
        return cur

    def executemany(self, sql, seq_of_parameters, /):
        self._note(sql)
        t0 = time.monotonic()
        try:
            cur = super().executemany(sql, seq_of_parameters)
        except sqlite3.OperationalError as e:
            elapsed = time.monotonic() - t0
            if "locked" in str(e) or elapsed >= self._DIAG_MIN_S:
                self._diag("executemany", sql, elapsed, str(e))
            raise
        elapsed = time.monotonic() - t0
        if elapsed >= self._DIAG_MIN_S:
            self._diag("executemany", sql, elapsed)
        return cur


def connect(path: Optional[str | Path] = None,
            busy_timeout_ms: int = 30000) -> sqlite3.Connection:
    ensure_dirs()
    p = str(path or DB_PATH)
    conn = sqlite3.connect(p, timeout=max(30, busy_timeout_ms // 1000),
                           factory=_TimedConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)};")
    # Cache/mmap sizing (2026-10-18, revised twice on live evidence):
    # the weather_forecasts indexes alone are ~142 MB (autoindex 91.7 MB
    # + idx_wf_loc_time 49.9 MB, dbstat). The default 2 MB cache — and
    # even the 64 MB tried first — cannot hold that hot set, so every
    # INSERT OR IGNORE / AQ UPDATE became random-IO probes; when the
    # disk is busy (e.g. Sheets scanning its snapshot), probe latency
    # explodes, write transactions stretch to minutes, and other
    # writers' busy_timeout expires — the "database is locked" storms.
    # Isolated benchmarks are clean (672-row insert 0.14 s; 4-thread
    # repro 0.3 s, no locks), and a 1-worker production run still
    # stormed, so this is IO-latency amplification, not write cost or
    # thread count. 192 MB cache keeps the index set resident.
    # cache_size (2026-10-18, revised): 192 MB per connection was tried
    # when the working theory was a single connection starved by the
    # 2 MB default. With FIVE concurrent collector connections (plus
    # API + pass stages) on a 7.9 GB no-swap VM, 5x192 MB of SQLite
    # cache thrashes the OS page cache that the 788 MB DB actually
    # relies on — write batches still stretched 15-20 s (sentinel).
    # 48 MB per connection (24x the SQLite default) keeps each
    # connection's hot index pages resident without starving the OS.
    conn.execute("PRAGMA cache_size=-49152;")
    # wal_autocheckpoint (2026-10-18, third revision — read carefully):
    # an autocheckpoint runs INSIDE the committing connection while it
    # holds the write lock. At the default 1000 pages the inline copy
    # already stretched commits under this VM's disk load; at 20000
    # pages (~80 MB WAL observed) commits stalled ~120 s and a
    # collector run wrote 0 rows — the "fix" made it worse. Correct
    # shape: commits NEVER checkpoint inline (backstop only, set huge);
    # scripts/lock_watchdog.py issues wal_checkpoint(PASSIVE) from its
    # own connection every ~30 s, which copies without holding the
    # write lock hostage.
    conn.execute("PRAGMA wal_autocheckpoint=100000;")
    conn.execute("PRAGMA temp_store=MEMORY;")
    conn.execute("PRAGMA mmap_size=268435456;")
    # synchronous=NORMAL (added 2026-10-18): with WAL this stays
    # corruption-safe (only an OS crash/power loss can drop the last
    # committed transactions; hourly Drive backups bound that loss) and
    # removes the per-commit fsync cost under concurrent writers.
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def init_db(path: Optional[str | Path] = None) -> str:
    """Create (or migrate idempotently) the DB from schema.sql. Returns the db path."""
    ensure_dirs()
    p = Path(path or DB_PATH)
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    conn = connect(p)
    try:
        conn.executescript(sql)
        conn.commit()
        from backend.db.migrate import ensure_v2
        ensure_v2(conn)  # additive only: new columns/tables for existing DBs
    finally:
        conn.close()
    return str(p)


def execute(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
    cur = conn.execute(sql, params)
    return cur


def executemany(conn: sqlite3.Connection, sql: str, rows: Iterable[Sequence[Any]]) -> sqlite3.Cursor:
    cur = conn.executemany(sql, rows)
    return cur


def query(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> list[dict]:
    cur = conn.execute(sql, params)
    return [dict(r) for r in cur.fetchall()]


def query_one(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> Optional[dict]:
    cur = conn.execute(sql, params)
    r = cur.fetchone()
    return dict(r) if r else None


def table_count(conn: sqlite3.Connection, table: str) -> int:
    row = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()
    return int(row["c"])
