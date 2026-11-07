#!/usr/bin/env python3
"""AETHER Earth Intelligence — single entry point.

Thin dispatcher over the scripts/ stages (the real code lives there):

    python main.py collect       # 5-minute advanced collector run
    python main.py system        # full hourly one-system pass
    python main.py api           # FastAPI delivery service (127.0.0.1:8000)
    python main.py sync-sheets   # Google Sheets mirror rebuild
    python main.py gdacs         # GDACS disaster-feed collection
    python main.py train         # ML train cycle (1 GiB growth trigger)

Every command is idempotent where the underlying stage is; see docs/OPERATIONS.md.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV_PY = str(ROOT / ".tooling" / "aether-venv" / "bin" / "python")
# Fall back to the workspace venv path used in production.
if not Path(VENV_PY).exists():
    VENV_PY = str(Path.home() / "workspace" / ".tooling" / "aether-venv" / "bin" / "python")

COMMANDS = {
    "collect": ["scripts/run_collect_advanced.py"],
    "system": ["scripts/run_system.py"],
    "api": ["scripts/run_api.py"],
    "sync-sheets": ["scripts/sync_sheets.py"],
    "gdacs": ["scripts/collect_gdacs.py"],
    "train": ["scripts/train_cycle.py"],
}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        print("commands:", ", ".join(sorted(COMMANDS)))
        return 2
    cmd = [VENV_PY, *(COMMANDS[sys.argv[1]]), *sys.argv[2:]]
    return subprocess.run(cmd, cwd=ROOT).returncode


if __name__ == "__main__":
    raise SystemExit(main())
