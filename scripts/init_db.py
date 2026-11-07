#!/usr/bin/env python3
"""Create data/aether.db from backend/db/schema.sql (idempotent)."""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from backend.common.config import DB_PATH, ensure_dirs  # noqa: E402
from backend.db import db as dbmod  # noqa: E402


def main() -> None:
    ensure_dirs()
    path = dbmod.init_db()
    conn = dbmod.connect(path)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()]
    conn.close()
    print(f"DB initialized: {path}")
    print(f"tables ({len(tables)}): {', '.join(tables)}")


if __name__ == "__main__":
    main()
