"""Acceptance 84.1 / 92 / 114 / 115: foundation, DB plans, secrets, provenance."""
from __future__ import annotations

import re
import sqlite3
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.api.routes import perf as perf_routes  # noqa: E402
from backend.db import db as dbmod  # noqa: E402
from backend.db import migrate_perf  # noqa: E402
from backend.perf import metrics  # noqa: E402
from tests.test_acceptance_lib import verdict  # noqa: E402


@pytest.fixture()
def full_db(tmp_path):
    db = str(tmp_path / "full.db")
    dbmod.init_db(db)
    migrate_perf.apply(db)
    conn = dbmod.connect(db)
    conn.execute(
        "INSERT INTO locations (id,name,latitude,longitude,timezone,country,created_at)"
        " VALUES (1,'Yangon',16.84,96.17,'Asia/Yangon','Myanmar','2026-01-01T00:00:00Z')")
    for i in range(200):
        conn.execute(
            """INSERT INTO earthquake_events (canonical_event_id,source,source_event_ids,
                origin_time,latitude,longitude,magnitude,region,created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (f"syn{i}", "usgs", '["us1"]', f"2026-10-04T{(i % 24):02d}:00:00Z",
             16.0 + i * 0.01, 96.0 + i * 0.01, 4.0 + (i % 40) / 10.0,
             "Myanmar", "2026-10-04T00:00:00Z"))
    for i in range(100):
        conn.execute(
            """INSERT INTO weather_forecasts (location_id,source,model,forecast_run,
                forecast_hour,issued_at,valid_time,temperature,collected_at)
               VALUES (1,'open-meteo','gfs',printf('run-%03d',?),
                ?, '2026-10-05T00:00:00Z',printf('2026-10-05T%02d:00:00Z',?),
                30.0,'2026-10-05T00:00:00Z')""", (i, i % 48, i % 24))
    conn.commit()
    yield db, conn
    conn.close()


def test_84_1_build_migrations_startup(tmp_path):
    t0 = time.monotonic()
    db = str(tmp_path / "mig.db")
    dbmod.init_db(db)
    r1 = migrate_perf.apply(db)
    r2 = migrate_perf.apply(db)  # idempotent: second run must not error
    assert r1["tables"] == r2["tables"] and r1["indexes"] == r2["indexes"]
    assert perf_routes.router is not None
    routes = [r.path for r in perf_routes.router.routes]
    assert "/v1/home/{location_id}" in routes and "/v1/map/events" in routes
    import scripts.collect_fast  # noqa: F401 - fast runner must be importable (startup)
    dt = (time.monotonic() - t0) * 1000.0
    verdict(84.1, "PASS", {"migrate_tables": r1["tables"],
                           "migrate_indexes": r1["indexes"],
                           "routes_mounted": len(routes),
                           "startup_import_ms": round(dt, 1)})
    print(f"\n[84.1] migrations ok ({r1['tables']} tables, {r1['indexes']} indexes), "
          f"{len(routes)} perf routes, import {dt:.1f} ms")


def _plan_uses_index(conn, sql, params=()):
    plan = " ".join(r["detail"] for r in
                    conn.execute("EXPLAIN QUERY PLAN " + sql, params))
    return "USING INDEX" in plan or "USING COVERING INDEX" in plan, plan


def test_92_db_query_latency_and_explain_plans(full_db):
    db, conn = full_db
    checks = [
        ("quake time-range scan",
         "SELECT * FROM earthquake_events WHERE origin_time > '2026-10-04T00:00:00Z' ORDER BY origin_time DESC LIMIT 50"),
        ("quake magnitude filter",
         "SELECT * FROM earthquake_events WHERE magnitude >= 6.0"),
        ("alert status scan",
         "SELECT * FROM alerts WHERE status='active' ORDER BY issued_at DESC LIMIT 20"),
        ("forecast bundle fetch",
         "SELECT * FROM weather_forecasts WHERE location_id=1 AND model='gfs' ORDER BY valid_time LIMIT 48"),
        ("health freshness lookup",
         "SELECT * FROM system_health WHERE component='usgs-poll' ORDER BY checked_at DESC LIMIT 1"),
    ]
    latencies, plans = {}, {}
    for name, sql in checks:
        t0 = time.monotonic()
        conn.execute(sql).fetchall()
        latencies[name] = round((time.monotonic() - t0) * 1000.0, 2)
        ok, plan = _plan_uses_index(conn, sql)
        plans[name] = {"uses_index": ok, "plan": plan}
    indexed = sum(1 for p in plans.values() if p["uses_index"])
    assert indexed >= 4, plans  # at least 4 of 5 hot queries use an index
    slow = {k: v for k, v in latencies.items() if v > 50}
    assert not slow, slow  # 200-row table: everything must be <50 ms
    verdict(92, "PASS", {"queries": len(checks), "indexed": indexed,
                        "latencies_ms": latencies})
    print(f"\n[92] {indexed}/{len(checks)} hot queries use indexes; "
          f"worst latency {max(latencies.values())} ms")
    # honest note: PostGIS plan comparison is BLOCKED (SQLite deployment)
    verdict(92.5, "BLOCKED", blocked={
        "external_dependency": "PostGIS",
        "why_it_blocks": "deployment DB is SQLite; PostGIS query plans cannot be "
                         "measured here",
        "workaround": "documented as production plug-in in docs/PERFORMANCE.md"})


SECRET_PATTERNS = [
    (r"AKIA[0-9A-Z]{16}", "aws access key"),
    (r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----", "private key"),
    (r"(?i)(password|passwd|secret)\s*=\s*['\"][^'\"]{4,}['\"]", "hardcoded credential"),
    (r"AIza[0-9A-Za-z_-]{35}", "google api key"),
    (r"xox[bap]-[0-9A-Za-z-]{10,}", "slack token"),
]


def test_114_no_secrets_in_new_files():
    new_files = list((ROOT / "backend" / "perf").glob("*.py")) + \
                list((ROOT / "backend" / "api" / "routes").glob("perf.py")) + \
                list((ROOT / "scripts").glob("collect_fast.py")) + \
                list((ROOT / "tests").glob("test_perf_*.py")) + \
                list((ROOT / "tests").glob("test_acceptance_*.py"))
    hits = []
    for f in new_files:
        text = f.read_text(encoding="utf-8", errors="ignore")
        for pat, label in SECRET_PATTERNS:
            for m in re.finditer(pat, text):
                hits.append((f.name, label, m.group(0)[:12] + "..."))
    assert not hits, hits
    verdict(114, "PASS", {"files_scanned": len(new_files), "hits": 0})
    print(f"\n[114] {len(new_files)} new files scanned, 0 secret hits")


def test_115_provenance_present(full_db):
    from backend.perf import precompute
    db, conn = full_db
    b = precompute.build_home_bundle(conn, 1)
    assert b["provenance"]["sources"] == ["aether-db"]
    assert b["provenance"]["built_at"]
    assert b["freshness"]["earthquake"]["verified"] in (True, False)
    verdict(115, "PASS", {"provenance_sources": b["provenance"]["sources"],
                          "freshness_blocks": list(b["freshness"].keys())})
    print("\n[115] home bundle carries provenance + per-feed freshness blocks")
