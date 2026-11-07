#!/usr/bin/env python3
"""Audit of EXISTING stored data (one-shot, read-only except audit records).

The advanced collector's validation gate protects NEW rows. This script
applies the same checks to everything already in the database:

  1. Range validation over every weather_forecasts row (and the matching
     fields of weather_observations) using the gate's own rules
     (backend.collectors.advanced.value_in_range). Violations are COUNTED
     and sampled — never deleted, never modified (training data rule).
  2. Full-history gap scan: per (location, model), every hour between
     the first and last stored valid_time that has no forecast row.
  3. SQLite integrity_check + foreign_key_check + ANALYZE via
     backend.datamgmt.maintenance.

Findings are written to the data_quality table (component
'existing_data_audit') and a JSON summary is printed.
"""
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from backend.collectors.advanced import value_in_range  # noqa: E402
from backend.common.timeutil import utc_iso  # noqa: E402
from backend.datamgmt import maintenance  # noqa: E402
from backend.db import db as dbmod  # noqa: E402

FIELDS = ("temperature", "humidity", "pressure", "wind_speed", "precipitation")


def audit_ranges(conn) -> dict:
    out = {}
    for table in ("weather_forecasts", "weather_observations"):
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        fields = [f for f in FIELDS if f in cols]
        total = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        violations = Counter()
        by_location = Counter()
        samples = {}
        loc_col = "location_id" if "location_id" in cols else None
        sel = ", ".join(fields + ([loc_col] if loc_col else []))
        for row in conn.execute(f"SELECT {sel} FROM {table}"):
            d = dict(zip(fields + ([loc_col] if loc_col else []), row))
            for f in fields:
                if not value_in_range(f, d[f]):
                    violations[f] += 1
                    if loc_col:
                        by_location[d[loc_col]] += 1
                    samples.setdefault(f, d[f])
        out[table] = {
            "rows": total,
            "violations_by_field": dict(violations),
            "violations_total": int(sum(violations.values())),
            "violations_by_location": {str(k): v for k, v in
                                       by_location.most_common(10)},
            "sample_values": samples,
        }
    return out


def audit_gaps(conn) -> dict:
    names = {r[0]: r[1] for r in conn.execute("SELECT id, name FROM locations")}
    total_missing = 0
    per_location = {}
    blocks = Counter()
    combos = conn.execute(
        "SELECT location_id, model, MIN(valid_time), MAX(valid_time) "
        "FROM weather_forecasts GROUP BY location_id, model").fetchall()
    for loc_id, model, vmin, vmax in combos:
        have = {r[0] for r in conn.execute(
            "SELECT DISTINCT valid_time FROM weather_forecasts "
            "WHERE location_id=? AND model=?", (loc_id, model))}
        t = datetime.fromisoformat(vmin.replace("Z", "+00:00"))
        end = datetime.fromisoformat(vmax.replace("Z", "+00:00"))
        missing = 0
        while t <= end:
            iso = t.strftime("%Y-%m-%dT%H:00:00Z")
            if iso not in have:
                missing += 1
                blocks[iso[:10]] += 1
            t += timedelta(hours=1)
        if missing:
            total_missing += missing
            per_location.setdefault(names.get(loc_id, str(loc_id)), 0)
            per_location[names.get(loc_id, str(loc_id))] += missing
    return {
        "missing_hours_total": total_missing,
        "per_location_top": dict(sorted(per_location.items(),
                                        key=lambda kv: -kv[1])[:10]),
        "missing_by_day_top": dict(blocks.most_common(5)),
    }


def main() -> int:
    conn = dbmod.connect()
    now = utc_iso()
    ranges = audit_ranges(conn)
    gaps = audit_gaps(conn)
    integrity = maintenance.integrity_check(conn)
    fk = maintenance.foreign_key_check(conn)
    maintenance.analyze(conn)
    summary = {
        "as_of": now,
        "range_audit": ranges,
        "gap_audit": gaps,
        "integrity_check": integrity,
        "foreign_key_violations": fk,
    }
    for check, payload in (
        ("range_validation", ranges),
        ("gap_scan_full_history", gaps),
        ("integrity", {"integrity": integrity, "fk_violations": fk}),
    ):
        bad = (payload.get("violations_total", 0) if check == "range_validation"
               else payload.get("missing_hours_total", 0)
               if check == "gap_scan_full_history"
               else 0 if integrity == "ok" and not fk else 1)
        conn.execute(
            "INSERT INTO data_quality (component, check_name, status, details,"
            " checked_at) VALUES (?,?,?,?,?)",
            ("existing_data_audit", check, "warn" if bad else "ok",
             json.dumps(payload)[:4000], now))
    conn.commit()
    conn.close()
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
