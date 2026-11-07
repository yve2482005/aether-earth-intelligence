#!/usr/bin/env python3
"""Launch the AETHER API (uvicorn)."""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import uvicorn  # noqa: E402

from backend.common.config import API_HOST, API_PORT  # noqa: E402


def main() -> None:
    uvicorn.run("backend.api.app:app", host=API_HOST, port=API_PORT, log_level="info")


if __name__ == "__main__":
    main()
