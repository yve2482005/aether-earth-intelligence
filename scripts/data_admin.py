#!/usr/bin/env python3
"""AETHER data administration CLI (read-only).

Subcommands (all use the same backend.datamgmt functions as the
/v1/datamgmt API):

  overview                 JSON inventory of every table
  quality                  JSON per-dataset quality report
  integrity                integrity_check + foreign_key_check result
  export <table> <path>    CSV export of a table to a file (capped)

There is deliberately no delete/prune/update subcommand: training data
is never deleted. Uses the production DB unless --db PATH is given.
"""
import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from backend.datamgmt import export as export_mod  # noqa: E402
from backend.datamgmt import inventory, maintenance, quality  # noqa: E402
from backend.db import db as dbmod  # noqa: E402


def _connect(args) -> object:
    return dbmod.connect(args.db) if args.db else dbmod.connect()


def cmd_overview(args) -> int:
    conn = _connect(args)
    try:
        inv = inventory.full_inventory(conn)
    finally:
        conn.close()
    print(json.dumps(inv, indent=1))
    return 0


def cmd_quality(args) -> int:
    conn = _connect(args)
    try:
        out = quality.all_quality(conn)
    finally:
        conn.close()
    print(json.dumps(out, indent=1))
    return 0


def cmd_integrity(args) -> int:
    conn = _connect(args)
    try:
        out = maintenance.full_check(conn)
    finally:
        conn.close()
    print(json.dumps(out, indent=1))
    return 0 if out["ok"] else 1


def cmd_export(args) -> int:
    conn = _connect(args)
    try:
        text = export_mod.export_csv_text(conn, args.table, args.limit)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()
    Path(args.path).write_text(text, encoding="utf-8")
    lines = text.count("\n")
    print(f"exported {args.table} -> {args.path} ({lines} lines incl. header)")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=None,
                        help="database path (default: production DB)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("overview")
    sub.add_parser("quality")
    sub.add_parser("integrity")
    p_export = sub.add_parser("export")
    p_export.add_argument("table")
    p_export.add_argument("path")
    p_export.add_argument("--limit", type=int, default=None,
                          help=f"row cap (default {export_mod.DEFAULT_ROW_CAP})")
    args = parser.parse_args(argv)
    handler = {"overview": cmd_overview, "quality": cmd_quality,
               "integrity": cmd_integrity, "export": cmd_export}[args.cmd]
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
