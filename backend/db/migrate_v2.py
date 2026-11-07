"""AETHER v2 additive DB migration (idempotent).

Creates the NEW v2 tables needed by other v2 workstreams. Existing tables are
never altered, renamed, or dropped — every statement is CREATE TABLE IF NOT
EXISTS, so running this twice (or on a DB built from schema.sql, which
already contains these definitions) is a safe no-op.

Usage:
    python -m backend.db.migrate_v2            # migrate the default DB
    AETHER_DB_PATH=/path/to.db python -m backend.db.migrate_v2
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.common.logging_setup import setup_logging  # noqa: E402
from backend.common.timeutil import utc_iso  # noqa: E402
from backend.db import db as dbmod  # noqa: E402

log = setup_logging("aether.migrate_v2")

# ---------------------------------------------------------------------------
# New v2 tables (mirrors the appended definitions in backend/db/schema.sql).
# Additive only — no changes to existing tables.
# ---------------------------------------------------------------------------

DDL_ALERT_REVISIONS = """CREATE TABLE IF NOT EXISTS alert_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id TEXT NOT NULL REFERENCES alerts(alert_id),
    revision INTEGER NOT NULL,
    title TEXT, message TEXT, severity TEXT, status TEXT,
    changed_by TEXT, changed_at TEXT NOT NULL, reason TEXT,
    UNIQUE(alert_id, revision)
)"""

DDL_NOTIFICATION_HISTORY = """CREATE TABLE IF NOT EXISTS notification_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id TEXT REFERENCES alerts(alert_id),
    channel TEXT NOT NULL,               -- push|webhook|sms|email
    recipient TEXT,
    title TEXT, body TEXT,
    status TEXT NOT NULL DEFAULT 'queued', -- queued|sent|failed|skipped
    error TEXT,
    created_at TEXT NOT NULL, sent_at TEXT
)"""

DDL_NOTIFICATION_DELIVERIES = """CREATE TABLE IF NOT EXISTS notification_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    notification_id INTEGER NOT NULL REFERENCES notification_history(id),
    channel TEXT NOT NULL,
    provider_message_id TEXT,
    delivered_at TEXT,
    status TEXT NOT NULL,                -- delivered|failed|unknown
    detail TEXT
)"""

DDL_INCIDENTS = """CREATE TABLE IF NOT EXISTS incidents (
    incident_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    component TEXT,
    severity TEXT,                       -- info|minor|major|critical
    status TEXT NOT NULL DEFAULT 'open', -- open|investigating|mitigated|resolved|closed
    started_at TEXT NOT NULL, ended_at TEXT,
    summary TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
)"""

DDL_DEVICES = """CREATE TABLE IF NOT EXISTS devices (
    device_id TEXT PRIMARY KEY,
    user_id TEXT REFERENCES users(id),
    platform TEXT NOT NULL,              -- android|web|ios
    push_token TEXT,
    app_version TEXT,
    last_seen TEXT, created_at TEXT NOT NULL
)"""

DDL_ALERT_PREFERENCES = """CREATE TABLE IF NOT EXISTS alert_preferences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT REFERENCES users(id),
    device_id TEXT REFERENCES devices(device_id),
    channel TEXT NOT NULL,
    min_severity TEXT DEFAULT 'advisory', -- info|advisory|watch|warning|critical
    alert_types TEXT,                    -- JSON array of alert type strings
    enabled INTEGER DEFAULT 1,
    updated_at TEXT NOT NULL,
    UNIQUE(user_id, device_id, channel)
)"""

DDL_DATA_QUALITY = """CREATE TABLE IF NOT EXISTS data_quality (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    component TEXT NOT NULL,             -- collector:<source_id>|validate-data|api
    check_name TEXT NOT NULL,
    status TEXT NOT NULL,                -- pass|fail|warn
    details TEXT,                        -- JSON with counts / samples
    checked_at TEXT NOT NULL
)"""

DDL_INGESTION_RUNS = """CREATE TABLE IF NOT EXISTS ingestion_runs (
    run_id TEXT PRIMARY KEY,
    component TEXT NOT NULL,
    started_at TEXT NOT NULL, ended_at TEXT,
    status TEXT NOT NULL,                -- running|success|failed|partial
    rows_written INTEGER DEFAULT 0,
    error TEXT,
    created_at TEXT NOT NULL
)"""

IDX_DATA_QUALITY = ("idx_dq_comp_time",
                    "CREATE INDEX IF NOT EXISTS idx_dq_comp_time "
                    "ON data_quality(component, checked_at)")
IDX_INGESTION_RUNS = ("idx_ir_comp_time",
                      "CREATE INDEX IF NOT EXISTS idx_ir_comp_time "
                      "ON ingestion_runs(component, started_at)")
IDX_NOTIF_HISTORY = ("idx_nh_alert",
                     "CREATE INDEX IF NOT EXISTS idx_nh_alert "
                     "ON notification_history(alert_id, created_at)")

V2_DDL = (
    ("alert_revisions", DDL_ALERT_REVISIONS),
    ("notification_history", DDL_NOTIFICATION_HISTORY),
    ("notification_deliveries", DDL_NOTIFICATION_DELIVERIES),
    ("incidents", DDL_INCIDENTS),
    ("devices", DDL_DEVICES),
    ("alert_preferences", DDL_ALERT_PREFERENCES),
    ("data_quality", DDL_DATA_QUALITY),
    ("ingestion_runs", DDL_INGESTION_RUNS),
)
V2_INDEXES = (IDX_DATA_QUALITY, IDX_INGESTION_RUNS, IDX_NOTIF_HISTORY)


def migrate(path=None) -> dict:
    """Run the v2 migration idempotently. Returns {table: 'created'|'exists'}."""
    conn = dbmod.connect(path)
    result = {}
    try:
        for name, ddl in V2_DDL:
            conn.execute(ddl)
            result[name] = "created"
        for _iname, idx_sql in V2_INDEXES:
            conn.execute(idx_sql)
        conn.commit()
        # touch a marker so operators can tell the migration ran — only when
        # system_health exists (a bare migration DB may not have it yet)
        has_health = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='system_health'"
        ).fetchone()
        if has_health:
            conn.execute(
                """INSERT INTO system_health (component, status, message, checked_at)
                   VALUES ('db:migrate_v2', 'ok', 'v2 migration applied', ?)""",
                (utc_iso(),))
            conn.commit()
    finally:
        conn.close()
    log.info("v2 migration complete: %d tables ensured", len(result))
    return result


def main() -> None:
    from backend.common.config import DB_PATH  # noqa: E402
    result = migrate()
    conn = dbmod.connect()
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()]
    finally:
        conn.close()
    v2_tables = sorted(set(result) & set(tables))
    print(f"v2 migration applied to {DB_PATH}")
    print(f"v2 tables ensured ({len(v2_tables)}): {', '.join(v2_tables)}")


if __name__ == "__main__":
    main()
