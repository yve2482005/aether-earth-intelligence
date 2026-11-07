"""CSV export for any real table, with a row cap.

The table name is validated against sqlite_master before it is ever
interpolated into SQL (SQLite cannot bind identifiers), which is the
injection guard for this module.
"""
from __future__ import annotations

import csv
import io
import sqlite3
from typing import Iterator, List

from backend.datamgmt.inventory import column_names, require_table

DEFAULT_ROW_CAP = 10_000
MAX_ROW_CAP = 100_000


def normalize_cap(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_ROW_CAP
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return DEFAULT_ROW_CAP
    return min(max(n, 1), MAX_ROW_CAP)


def export_csv(conn: sqlite3.Connection, table: str,
               limit: int | None = None) -> Iterator[str]:
    """Yield CSV text chunks (header + rows) for `table`, capped.

    Raises ValueError for an unknown table.
    """
    require_table(conn, table)
    cap = normalize_cap(limit)
    cols = column_names(conn, table)
    cur = conn.execute(f'SELECT * FROM "{table}" LIMIT ?', (cap,))

    def _gen() -> Iterator[str]:
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(cols)
        yield buf.getvalue()
        for row in cur:
            buf.seek(0)
            buf.truncate(0)
            writer.writerow(["" if v is None else v for v in row])
            yield buf.getvalue()

    return _gen()


def export_csv_text(conn: sqlite3.Connection, table: str,
                    limit: int | None = None) -> str:
    return "".join(export_csv(conn, table, limit))


def export_columns(conn: sqlite3.Connection, table: str) -> List[str]:
    require_table(conn, table)
    return column_names(conn, table)
