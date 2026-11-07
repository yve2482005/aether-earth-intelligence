#!/usr/bin/env python3
"""Mirror AETHER data from SQLite into Google Sheets (full mirror).

Layout:
  * Main workbook — EVERY database table in full, except the two
    firehoses (weather_forecasts, earthquake_updates). Each tab is
    bounded by a per-tab safety guard (sheets.ROW_GUARD newest rows);
    a truncated tab is marked in the catalog.
  * Daily archive workbooks (AETHER_Data_YYYY-MM-DD) — the firehoses,
    one workbook per completed UTC day (sheets_archive).
  * AETHER_Training_Data — the full ML training pairs snapshot,
    replaced only when dataset_version changes (sheets_archive).
  * A `catalog` tab in the main workbook lists every dataset: DB rows,
    rows in Sheets, where it lives, last synced time.

Phase order (changed 2026-10-19): the daily archive runs FIRST — it is
deadline-critical (a completed day waits on it), while the main mirror
is a convenience copy. The mirror also skips any tab whose table is
unchanged since its last successful push (change signature in the
archive state file's `mirror` section), so a pass no longer spends
most of its 3300s stage budget re-pushing ~110k unchanged rows before
the archive gets a turn.

Safe to run after every collection; all steps are idempotent.
"""
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from backend.common.logging_setup import setup_logging  # noqa: E402
from backend.db import db as dbmod  # noqa: E402
from backend.integrations import sheets as sheets_mod  # noqa: E402
from backend.integrations import sheets_archive  # noqa: E402
from backend.integrations.sheets import (  # noqa: E402
    CATALOG_TAB,
    FIREHOSE_TABLES,
    ROW_GUARD,
    TAB_MAP,
    SheetMirror,
)

log = setup_logging("aether.sync_sheets")


def full_rows(conn, table: str, guard: int = ROW_GUARD):
    """Return (rows, total, truncated) for `table`.

    Full table while it fits the guard; otherwise the newest `guard`
    rows (in chronological/rowid order) with truncated=True. Missing
    tables yield ([], 0, False).
    """
    try:
        total = conn.execute(
            f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
    except Exception as exc:  # table missing etc. -> skip honestly
        log.warning("sync_sheets: cannot read %s: %s", table, exc)
        return [], 0, False
    if total <= guard:
        rows = dbmod.query(conn, f"SELECT * FROM {table} ORDER BY rowid")
        return rows, total, False
    rows = dbmod.query(
        conn, f"SELECT * FROM {table} ORDER BY rowid DESC LIMIT ?", (guard,))
    rows.reverse()
    return rows, total, True


def table_columns(conn, table: str):
    try:
        return [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
    except Exception:  # noqa: BLE001
        return []


# ------------------------------------------------------- mirror change skips
# A mirrored tab is skipped when its table's signature matches the one
# recorded after its last successful push. The signature is cheap: row
# count + max(rowid) + max of the table's main timestamp column (first
# match from the preference list; tables without one fall back to
# count + rowid, which still catches inserts and deletes). Signatures
# live in the `mirror` section of the archive state file.
_SIGNATURE_TS_CANDIDATES = (
    "updated_at", "collected_at", "received_at", "created_at",
    "issued_at", "delivered_at", "started_at", "deployed_at",
    "checked_at", "origin_time", "occurred_at", "event_time",
    "observed_at", "valid_time", "forecast_time", "sent_at",
    "last_success", "last_seen", "period_start", "date",
)


def mirror_signature(conn, table: str):
    """Change signature for one mirrored table, or None when the table
    is unreadable — never skip a tab on an uncertain signature."""
    try:
        row = conn.execute(
            f"SELECT COUNT(*) AS c, MAX(rowid) AS m FROM {table}").fetchone()
        cols = table_columns(conn, table)
        ts_col = next((c for c in _SIGNATURE_TS_CANDIDATES if c in cols),
                      None)
        ts_max = None
        if ts_col is not None:
            ts_max = conn.execute(
                f"SELECT MAX({ts_col}) AS v FROM {table}").fetchone()["v"]
        return {"rows": row["c"], "max_rowid": row["m"],
                "ts_col": ts_col, "ts_max": ts_max}
    except Exception:  # noqa: BLE001 - missing table etc. -> always push
        return None


def _recorded_signatures() -> dict:
    state = sheets_archive.load_state()
    return dict((state.get("mirror") or {}).get("tabs") or {})


def _record_signature(table: str, tab: str, signature) -> None:
    """Record a tab's signature after its successful push.

    Reload-merge-save: the archive/training phase children write the
    same state file during their own phases, so this never saves a
    stale in-memory copy over their updates.
    """
    state = sheets_archive.load_state()
    tabs = state.setdefault("mirror", {}).setdefault("tabs", {})
    tabs[table] = {
        "tab": tab,
        "signature": signature,
        "pushed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    sheets_archive.save_state(state)


def mirror_tables(conn, mirror) -> dict:
    """Push every TAB_MAP table into the main workbook; return stats.

    Tabs whose signature matches the last successful push are skipped
    (stats status "skipped_unchanged"; their sheet_rows are derived
    from the signature, which describes exactly what was pushed).
    A tab's new signature is recorded only after its push succeeds.
    """
    recorded = _recorded_signatures()
    stats = {}
    for table, tab in TAB_MAP.items():
        signature = mirror_signature(conn, table)
        prev = (recorded.get(table) or {}).get("signature")
        if signature is not None and prev == signature:
            total = signature["rows"]
            truncated = total > ROW_GUARD
            sheet_rows = min(total, ROW_GUARD)
            stats[table] = {"tab": tab, "sheet_rows": sheet_rows,
                            "db_rows": total, "truncated": truncated,
                            "status": "skipped_unchanged"}
            print(f"  {table:24s} -> {tab:22s} {'skipped':8s} "
                  f"rows={sheet_rows}/{total} (unchanged)"
                  + (" TRUNCATED(newest %d)" % ROW_GUARD if truncated else ""))
            continue
        rows, total, truncated = full_rows(conn, table)
        if rows:
            out = mirror.push_summary(table, rows)
        else:
            out = mirror.write_headers(tab, table_columns(conn, table))
        stats[table] = {"tab": tab, "sheet_rows": len(rows),
                        "db_rows": total, "truncated": truncated,
                        "status": out.get("status")}
        print(f"  {table:24s} -> {tab:22s} {out.get('status'):8s} "
              f"rows={len(rows)}/{total}"
              + (" TRUNCATED(newest %d)" % ROW_GUARD if truncated else ""))
        if signature is not None and out.get("status") == "ok":
            _record_signature(table, tab, signature)
            recorded[table] = {"signature": signature}
    return stats


def all_db_tables(conn):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
        " AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
    return [r["name"] for r in rows]


def build_catalog(conn, stats, archive_state, training_info, synced_at):
    """One catalog row per dataset (every DB table + training pairs)."""
    archived = (archive_state or {}).get("archived_days", {})
    arch_days = sorted(archived)
    rows = []
    for table in all_db_tables(conn):
        try:
            total = conn.execute(
                f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
        except Exception:  # noqa: BLE001
            total = 0
        if table in TAB_MAP:
            st = stats.get(table, {})
            note = ""
            if st.get("truncated"):
                note = ("TRUNCATED: newest %d of %d rows (per-tab guard)"
                        % (ROW_GUARD, total))
            rows.append({
                "dataset": table,
                "db_rows": total,
                "sheet_rows": st.get("sheet_rows", 0),
                "location": "main workbook tab '%s'" % TAB_MAP[table],
                "last_synced_at": synced_at,
                "note": note,
            })
        elif table in FIREHOSE_TABLES:
            if table == "weather_forecasts":
                sheet_rows = sum(d.get("forecast_rows", 0)
                                 for d in archived.values())
            else:
                sheet_rows = sum(d.get("update_rows", 0)
                                 for d in archived.values())
            if arch_days:
                location = ("daily workbooks %s_%s .. %s_%s (%d days)"
                            % (sheets_archive.ARCHIVE_PREFIX, arch_days[0],
                               sheets_archive.ARCHIVE_PREFIX, arch_days[-1],
                               len(arch_days)))
                note = "complete UTC days only; today is archived tomorrow"
            else:
                location = "daily workbooks %s_YYYY-MM-DD (none yet)" \
                    % sheets_archive.ARCHIVE_PREFIX
                note = ""
            rows.append({
                "dataset": table,
                "db_rows": total,
                "sheet_rows": sheet_rows,
                "location": location,
                "last_synced_at": synced_at,
                "note": note,
            })
        else:
            rows.append({
                "dataset": table,
                "db_rows": total,
                "sheet_rows": 0,
                "location": "not mirrored",
                "last_synced_at": synced_at,
                "note": "no mirror target defined",
            })
    if training_info:
        rows.append({
            "dataset": "training_pairs",
            "db_rows": training_info.get("pair_rows", 0),
            "sheet_rows": training_info.get("pair_rows", 0),
            "location": "workbook '%s'" % sheets_archive.TRAINING_WORKBOOK,
            "last_synced_at": synced_at,
            "note": ("derived dataset (ml/weather/dataset.py build_dataset);"
                     " snapshot %s; %s"
                     % (training_info.get("dataset_version", "?"),
                        training_info.get("status", ""))),
        })
    return rows


def _open_snapshot():
    """Return (conn, path) for a consistent snapshot of the live DB.

    Sync/archive state lives in bootstrap/SHEETS_ARCHIVE_STATE.json —
    never in the DB — and this script only READS the database, so it can
    read a snapshot taken at sync start (sqlite3 backup API) instead of
    holding a read connection on the live DB for the whole run. That keeps
    the long sync (hundreds of Sheets calls) off the live write path the
    collectors are committing to. Caller closes conn and removes the file
    via _remove_snapshot().
    """
    from backend.common.config import DATA_DIR

    snap_path = DATA_DIR / (".sheets_snapshot_%d.db" % os.getpid())
    live = dbmod.connect()
    try:
        raw = sqlite3.connect(str(snap_path))
        try:
            live.backup(raw)
        finally:
            raw.close()
    finally:
        live.close()
    # Page-cache protection (2026-10-18): the backup just streamed the
    # whole ~788 MB DB through the OS page cache. On this 7.9 GB
    # no-swap VM that evicts the collector's hot index pages, and its
    # write batches then fault from a very slow disk — the lock-storm
    # spiral. The snapshot itself is read sequentially by the sync
    # (the low-priority stage), so evict ITS pages: sync reads go to
    # disk, the live DB's pages stay resident.
    try:
        fd = os.open(str(snap_path), os.O_RDONLY)
        try:
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        finally:
            os.close(fd)
    except (OSError, AttributeError):
        pass
    return dbmod.connect(snap_path), snap_path


def _remove_snapshot(snap_path) -> None:
    for suffix in ("", "-wal", "-shm"):
        try:
            Path(str(snap_path) + suffix).unlink(missing_ok=True)
        except OSError:
            pass


def _sweep_orphan_snapshots() -> None:
    """Delete snapshot files left by sync processes that were killed
    (stage-timeout SIGKILL skips the finally cleanup in main). Runs at
    every sync startup (first line of main).

    History: added 2026-10-18 as an auto-delete after three orphans
    totalling ~1.73 GB were found; the owner set it report-only the
    same day ("leave them"). On 2026-10-23 the owner chose option B:
    delete the accumulated dead snapshots AND authorize auto-delete
    of dead-PID snapshots at sync startup going forward.

    Safety: a file is deleted only when the pid embedded in its name
    is verifiably dead — os.kill(pid, 0) raises AND /proc/<pid> is
    absent at delete time. Any live pid (including a reused one)
    keeps its file; our own pid is always skipped. Removes the .db
    plus its -wal/-shm/-journal sidecars; touches nothing else.
    """
    import glob as _glob
    from backend.common.config import DATA_DIR
    for path in _glob.glob(str(DATA_DIR / ".sheets_snapshot_*.db")):
        try:
            pid = int(Path(path).name.split("_")[-1].split(".")[0])
        except (ValueError, IndexError):
            continue
        if pid == os.getpid():
            continue
        try:
            os.kill(pid, 0)
            continue  # owner (or a reused pid) still alive -> keep
        except OSError:
            pass  # pid dead -> orphan candidate
        if Path(f"/proc/{pid}").exists():
            continue  # a new process took the pid meanwhile -> keep
        removed_bytes = 0
        removed_files = 0
        for suffix in ("", "-wal", "-shm", "-journal"):
            p = Path(path + suffix)
            try:
                size = p.stat().st_size
            except OSError:
                continue
            try:
                p.unlink()
            except OSError:
                continue
            removed_bytes += size
            removed_files += 1
        if removed_files:
            print(f"orphan snapshot deleted (owner-approved auto-clean): "
                  f"{path} ({removed_bytes} bytes, {removed_files} files)")


def _run_phase(phase: str, snap_path) -> dict:
    """Run one heavy phase (archive / training) in a child process.

    Changed 2026-10-18: run in-process, the phases' peaks accumulated
    to ~2.3 GB RSS (dataset build + archive day loads) and starved the
    collectors of page cache until their writes hit the lock timeout.
    A child process returns all of its memory to the OS on exit. The
    child works on the parent's snapshot file and the shared state
    JSON; it must not remove the snapshot (the parent owns it).
    """
    import subprocess

    def _die_with_parent() -> None:
        # PR_SET_PDEATHSIG: if the stage timeout kills this parent, the
        # phase child must die too — an orphaned archive child kept
        # running at ~2 GB RSS for 52 minutes on 2026-10-18 and
        # collided with the next pass.
        try:
            import ctypes

            ctypes.CDLL("libc.so.6").prctl(1, 9)  # PR_SET_PDEATHSIG, SIGKILL
        except Exception:  # noqa: BLE001 - best-effort
            pass

    cmd = [sys.executable, str(Path(__file__).resolve()),
           "--phase", phase, "--snapshot", str(snap_path)]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          preexec_fn=_die_with_parent)
    sys.stdout.write(proc.stdout)
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    for line in reversed(proc.stdout.splitlines()):
        if line.startswith("RESULT_JSON:"):
            return json.loads(line[len("RESULT_JSON:"):])
    return {"status": "error",
            "reason": "phase %s exited %d without a result"
                      % (phase, proc.returncode)}


def _phase_main(phase: str, snap_path: str) -> None:
    if phase == "archive":
        conn = dbmod.connect(snap_path)
        try:
            result = sheets_archive.archive_pending_days(conn)
        finally:
            conn.close()
    elif phase == "training":
        result = sheets_archive.sync_training_workbook(db_path=snap_path)
    else:
        raise SystemExit("unknown phase %r" % phase)
    print("RESULT_JSON:" + json.dumps(result))


def main() -> None:
    _sweep_orphan_snapshots()
    mirror = SheetMirror()
    print("sheets enabled:", mirror.enabled)
    if not mirror.enabled:
        print("Sheets not configured/connected; nothing to do.")
        print(json.dumps(mirror.health(), indent=1))
        return
    print("ensure_tabs:", json.dumps(mirror.ensure_tabs()))
    synced_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn, snap_path = _open_snapshot()
    try:
        # Firehose daily archive FIRST (order changed 2026-10-19):
        # completed days are deadline-critical, and with tab-level
        # checkpointing the archive now finishes within its share of
        # the stage budget instead of starving behind a full mirror
        # re-push. Runs in a child process so its memory is released
        # before the later phases. A failure here must not kill the
        # sync: the catalog below is the run's record.
        try:
            archive = _run_phase("archive", snap_path)
        except Exception as exc:  # noqa: BLE001 - report, don't abort
            archive = {"status": "error", "reason": str(exc)[:200]}
        print("archive:", json.dumps(archive))

        # Main-workbook mirror (unchanged tabs skipped by signature).
        stats = mirror_tables(conn, mirror)

        # Training pairs snapshot (version-guarded inside), built from
        # the same DB snapshot as the rest of this run, also isolated.
        try:
            training = _run_phase("training", snap_path)
        except Exception as exc:  # noqa: BLE001 - report, don't abort
            training = {"status": "error", "reason": str(exc)[:200]}
        print("training:", json.dumps(training))

        state = sheets_archive.load_state()
        training_info = dict(state.get("training") or {})
        if training.get("status"):
            training_info.setdefault("status", training["status"])
            if training.get("dataset_version"):
                training_info["dataset_version"] = training["dataset_version"]
            if training.get("pair_rows"):
                training_info["pair_rows"] = training["pair_rows"]
        catalog = build_catalog(conn, stats, state, training_info, synced_at)
        out = mirror.push_tab(CATALOG_TAB, catalog)
        print(f"  {'catalog':24s} -> {CATALOG_TAB:22s} {out.get('status'):8s} "
              f"rows={out.get('rows')}")
    finally:
        conn.close()
        _remove_snapshot(snap_path)
    print("\nhealth:", json.dumps(mirror.health()))


if __name__ == "__main__":
    if "--phase" in sys.argv:
        _i = sys.argv.index("--phase")
        _j = sys.argv.index("--snapshot")
        _phase_main(sys.argv[_i + 1], sys.argv[_j + 1])
    else:
        main()
