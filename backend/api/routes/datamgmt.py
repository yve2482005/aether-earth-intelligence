"""GET/POST /v1/datamgmt/* — Advanced Data Management System.

The data layer beside /v1/admin (operations): full table inventory,
per-dataset quality, CSV export, SQLite maintenance and a self-contained
HTML dashboard.

READ-ONLY by design, apart from the maintenance verbs (integrity_check
and foreign_key_check are read-only; ANALYZE refreshes planner stats;
VACUUM rewrites the file without dropping data). There are deliberately
NO delete/prune/update endpoints: training data is never deleted.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse

from backend.api._util import provenance
from backend.common.config import DATA_DIR, DB_PATH, REPO_ROOT
from backend.datamgmt import export as export_mod
from backend.datamgmt import inventory, maintenance, quality
from backend.db import db as dbmod

router = APIRouter(prefix="/v1/datamgmt", tags=["datamgmt"])

# Directories where a local backup copy would live if one were kept.
# (In practice backups are uploaded straight to Google Drive by
# scripts/sync_drive.py; see the /backups endpoint note.)
_LOCAL_BACKUP_CANDIDATES = (DATA_DIR / "backups", REPO_ROOT / "backups")


def _conn():
    c = dbmod.connect()
    try:
        yield c
    finally:
        c.close()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------- overview ----------------

@router.get("/overview")
def overview(conn=Depends(_conn)) -> Dict[str, Any]:
    inv = inventory.full_inventory(conn)
    try:
        db_bytes = os.path.getsize(DB_PATH)
    except OSError:
        db_bytes = None
    return {
        "as_of": _now(),
        "database": {"path": str(DB_PATH), "size_bytes": db_bytes},
        "table_count": inv["table_count"],
        "total_rows": inv["total_rows"],
        "dbstat_available": inv["dbstat_available"],
        "tables": inv["tables"],
        "note": "Read-only inventory of every table in the database. "
                "size_bytes is the table's own dbstat pages (indexes are "
                "separate dbstat entries); null when dbstat is unavailable.",
        "provenance": provenance(["aether-datamgmt"], _now()),
    }


# ---------------- table row browsing ----------------

@router.get("/tables/{name}/rows")
def table_rows(name: str,
               limit: int = Query(50, ge=1, le=500),
               offset: int = Query(0, ge=0),
               order_by: Optional[str] = Query(None),
               order_dir: str = Query("asc"),
               conn=Depends(_conn)) -> Dict[str, Any]:
    if not inventory.table_exists(conn, name):
        raise HTTPException(404, f"unknown table: {name!r}")
    cols = inventory.column_names(conn, name)
    if order_by is not None and order_by not in cols:
        raise HTTPException(400, f"order_by must be one of {cols}")
    if order_dir.lower() not in ("asc", "desc"):
        raise HTTPException(400, "order_dir must be 'asc' or 'desc'")
    total = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
    sql = f'SELECT * FROM "{name}"'
    if order_by:
        sql += f' ORDER BY "{order_by}" {order_dir.upper()}'
    sql += " LIMIT ? OFFSET ?"
    rows = [dict(r) for r in conn.execute(sql, (limit, offset)).fetchall()]
    return {"table": name, "columns": cols, "rows": rows, "total": total,
            "limit": limit, "offset": offset,
            "order_by": order_by, "order_dir": order_dir.lower(),
            "provenance": provenance(["aether-datamgmt"], _now())}


# ---------------- quality ----------------

@router.get("/quality")
def quality_report(conn=Depends(_conn)) -> Dict[str, Any]:
    return {"as_of": _now(),
            "datasets": quality.all_quality(conn),
            "note": "NULL rates cover the declared key columns only. "
                    "duplicate_natural_key_rows counts rows beyond the "
                    "first that share the dataset's natural key.",
            "provenance": provenance(["aether-datamgmt"], _now())}


# ---------------- maintenance ----------------

@router.post("/maintenance/integrity")
def maintenance_integrity(conn=Depends(_conn)) -> Dict[str, Any]:
    out = maintenance.full_check(conn)
    out["provenance"] = provenance(["aether-datamgmt"], _now())
    return out


@router.post("/maintenance/analyze")
def maintenance_analyze(conn=Depends(_conn)) -> Dict[str, Any]:
    out = maintenance.analyze(conn)
    out["provenance"] = provenance(["aether-datamgmt"], _now())
    return out


@router.post("/maintenance/vacuum")
def maintenance_vacuum(conn=Depends(_conn)) -> Dict[str, Any]:
    # VACUUM cannot run inside a transaction and needs its own
    # connection; resolve the file from the request connection, then
    # run it on a fresh one (maintenance.vacuum).
    path = maintenance.db_file_path(conn)
    out = maintenance.vacuum(path)
    out["provenance"] = provenance(["aether-datamgmt"], _now())
    return out


# ---------------- export ----------------

@router.get("/export/{table}")
def export_table(table: str, limit: Optional[int] = Query(None),
                 conn=Depends(_conn)):
    if not inventory.table_exists(conn, table):
        raise HTTPException(404, f"unknown table: {table!r}")
    gen = export_mod.export_csv(conn, table, limit)
    headers = {"Content-Disposition": f'attachment; filename="{table}.csv"'}
    return StreamingResponse(gen, media_type="text/csv", headers=headers)


# ---------------- backups ----------------

@router.get("/backups")
def backups() -> Dict[str, Any]:
    local: List[Dict[str, Any]] = []
    found_dir: Optional[str] = None
    for cand in _LOCAL_BACKUP_CANDIDATES:
        if cand.is_dir():
            found_dir = str(cand)
            for f in sorted(Path(cand).glob("*.db")):
                st = f.stat()
                local.append({
                    "name": f.name, "size_bytes": st.st_size,
                    "modified_at": datetime.fromtimestamp(
                        st.st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                })
            break
    return {
        "as_of": _now(),
        "local_backup_dir": found_dir,
        "local_backups": local,
        "note": ("No local backup copies are kept. Database backups are "
                 "uploaded directly to Google Drive (BACKUPS/ folder, "
                 "timestamped aether-YYYYMMDD-HHMMSS.db) by the hourly "
                 "aether-sync job via scripts/sync_drive.py."
                 if not local else
                 "Local backup copies found on disk."),
        "provenance": provenance(["aether-datamgmt"], _now()),
    }


# ---------------- dashboard ----------------

_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AETHER — Data Management</title>
<style>
  :root { color-scheme: dark; }
  body { font-family: system-ui, sans-serif; margin: 0; background: #0d1117;
         color: #e6edf3; }
  header { padding: 16px 20px; background: #161b22; border-bottom: 1px solid #30363d; }
  header h1 { margin: 0; font-size: 20px; }
  header p { margin: 4px 0 0; color: #8b949e; font-size: 13px; }
  main { padding: 20px; max-width: 1100px; margin: 0 auto; }
  h2 { font-size: 16px; margin: 24px 0 8px; }
  table { border-collapse: collapse; width: 100%; font-size: 13px; }
  th, td { border: 1px solid #30363d; padding: 6px 8px; text-align: left; }
  th { background: #161b22; }
  tr:nth-child(even) td { background: #11161d; }
  .num { text-align: right; font-variant-numeric: tabular-nums; }
  .muted { color: #8b949e; }
  .err { color: #f85149; }
  .badge { display: inline-block; padding: 1px 7px; border-radius: 10px;
           background: #1f6feb; color: #fff; font-size: 11px; }
</style>
</head>
<body>
<header>
  <h1>AETHER — Advanced Data Management</h1>
  <p id="meta">Loading…</p>
</header>
<main>
  <h2>Table inventory</h2>
  <div id="inventory"><p class="muted">Loading…</p></div>
  <h2>Dataset quality &amp; freshness</h2>
  <div id="quality"><p class="muted">Loading…</p></div>
</main>
<script>
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
    return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];
  });
}
function fmtBytes(n) {
  if (n == null) return "—";
  if (n < 1024) return n + " B";
  if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
  return (n / 1048576).toFixed(1) + " MB";
}
async function load() {
  try {
    const [ov, q] = await Promise.all([
      fetch("/v1/datamgmt/overview").then(r => r.json()),
      fetch("/v1/datamgmt/quality").then(r => r.json())
    ]);
    document.getElementById("meta").innerHTML =
      esc(ov.table_count) + " tables · " + esc(ov.total_rows) +
      " total rows · database " + fmtBytes(ov.database.size_bytes) +
      " · as of " + esc(ov.as_of);
    let h = "<table><tr><th>Table</th><th class='num'>Rows</th>" +
            "<th class='num'>Size</th><th>Time column</th>" +
            "<th>Earliest</th><th>Latest</th><th></th></tr>";
    for (const t of ov.tables) {
      h += "<tr><td>" + esc(t.table) + "</td><td class='num'>" +
        esc(t.row_count) + "</td><td class='num'>" + fmtBytes(t.size_bytes) +
        "</td><td>" + esc(t.time_column || "—") + "</td><td>" +
        esc(t.time_min || "—") + "</td><td>" + esc(t.time_max || "—") +
        "</td><td><a href='/v1/datamgmt/export/" + esc(t.table) + "'>CSV</a>" +
        "</td></tr>";
    }
    document.getElementById("inventory").innerHTML = h + "</table>";
    let g = "<table><tr><th>Dataset</th><th class='num'>Rows</th>" +
            "<th>Latest</th><th class='num'>Age (min)</th>" +
            "<th class='num'>Duplicate keys</th><th>Max NULL rate</th></tr>";
    for (const name of Object.keys(q.datasets)) {
      const d = q.datasets[name];
      let maxNull = "—";
      if (d.null_rates) {
        let m = 0, seen = false;
        for (const k of Object.keys(d.null_rates)) {
          if (d.null_rates[k] != null) { seen = true; m = Math.max(m, d.null_rates[k]); }
        }
        if (seen) maxNull = (m * 100).toFixed(1) + "%";
      }
      g += "<tr><td>" + esc(name) + (d.present ? "" :
        " <span class='badge'>missing</span>") + "</td><td class='num'>" +
        esc(d.row_count == null ? "—" : d.row_count) + "</td><td>" +
        esc(d.latest_timestamp || "—") + "</td><td class='num'>" +
        esc(d.age_minutes == null ? "—" : d.age_minutes) +
        "</td><td class='num'>" +
        esc(d.duplicate_natural_key_rows == null ? "—" : d.duplicate_natural_key_rows) +
        "</td><td>" + esc(maxNull) + "</td></tr>";
    }
    document.getElementById("quality").innerHTML = g + "</table>";
  } catch (e) {
    document.getElementById("meta").innerHTML =
      "<span class='err'>Failed to load data: " + esc(e) + "</span>";
  }
}
load();
</script>
</body>
</html>
"""


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard() -> str:
    return _DASHBOARD_HTML
