"""SQLite maintenance verbs: integrity_check, foreign_key_check,
ANALYZE and VACUUM.

integrity_check / foreign_key_check are read-only. ANALYZE refreshes the
query-planner statistics tables. VACUUM rebuilds the database file
(reclaiming freelist pages) without dropping any data; it runs on a
fresh autocommit connection because VACUUM cannot execute inside a
transaction.
"""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict


def integrity_check(conn: sqlite3.Connection) -> Dict[str, Any]:
    rows = [r[0] for r in conn.execute("PRAGMA integrity_check").fetchall()]
    return {"integrity_check": rows, "ok": rows == ["ok"]}


def foreign_key_check(conn: sqlite3.Connection) -> Dict[str, Any]:
    rows = conn.execute("PRAGMA foreign_key_check").fetchall()
    violations = [{"table": r[0], "rowid": r[1], "parent": r[2], "fkid": r[3]}
                  for r in rows]
    return {"foreign_key_violations": violations, "ok": not violations}


def analyze(conn: sqlite3.Connection) -> Dict[str, Any]:
    t0 = time.monotonic()
    conn.execute("ANALYZE")
    conn.commit()
    return {"analyze": "ok",
            "duration_ms": round((time.monotonic() - t0) * 1000.0, 1)}


def db_file_path(conn: sqlite3.Connection) -> str:
    """Filesystem path of the connection's main database."""
    for _seq, name, path in conn.execute("PRAGMA database_list").fetchall():
        if name == "main":
            return path
    raise ValueError("connection has no main database file")


def vacuum(db_path: str | Path) -> Dict[str, Any]:
    """VACUUM the database file at `db_path` on a fresh connection.

    isolation_level=None puts the driver in autocommit mode so VACUUM is
    never inside a transaction. Returns file sizes before/after.
    """
    p = str(db_path)
    before = os.path.getsize(p) if os.path.exists(p) else None
    t0 = time.monotonic()
    conn = sqlite3.connect(p, timeout=120, isolation_level=None)
    try:
        conn.execute("PRAGMA busy_timeout=120000")
        conn.execute("VACUUM")
    finally:
        conn.close()
    after = os.path.getsize(p) if os.path.exists(p) else None
    return {"vacuum": "ok", "db_path": p,
            "size_bytes_before": before, "size_bytes_after": after,
            "duration_ms": round((time.monotonic() - t0) * 1000.0, 1)}


def full_check(conn: sqlite3.Connection) -> Dict[str, Any]:
    ic = integrity_check(conn)
    fk = foreign_key_check(conn)
    return {"integrity_check": ic["integrity_check"],
            "foreign_key_violations": fk["foreign_key_violations"],
            "ok": ic["ok"] and fk["ok"]}
