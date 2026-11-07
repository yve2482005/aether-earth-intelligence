"""AETHER backend configuration. Environment-driven only (AETHER_*). No secrets in code."""
from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

DATA_DIR = Path(os.environ.get("AETHER_DATA_DIR", str(REPO_ROOT / "data")))
DB_PATH = Path(os.environ.get("AETHER_DB_PATH", str(DATA_DIR / "aether.db")))
SCHEMA_PATH = REPO_ROOT / "backend" / "db" / "schema.sql"

API_HOST = os.environ.get("AETHER_API_HOST", "127.0.0.1")
API_PORT = int(os.environ.get("AETHER_API_PORT", "8000"))

DEFAULT_LAT = float(os.environ.get("AETHER_DEFAULT_LAT", "16.84"))   # Yangon
DEFAULT_LON = float(os.environ.get("AETHER_DEFAULT_LON", "96.17"))
DEFAULT_LOCATION_NAME = os.environ.get("AETHER_DEFAULT_LOCATION", "Yangon")

HTTP_TIMEOUT = float(os.environ.get("AETHER_HTTP_TIMEOUT", "25"))
HTTP_RETRIES = int(os.environ.get("AETHER_HTTP_RETRIES", "3"))
HTTP_BACKOFF = float(os.environ.get("AETHER_HTTP_BACKOFF", "1.5"))

# Google Sheets/Drive mirrors: disabled unless explicitly configured (integrations team owns).
SHEETS_ENABLED = os.environ.get("AETHER_SHEETS_ENABLED", "0") == "1"
DRIVE_ENABLED = os.environ.get("AETHER_DRIVE_ENABLED", "0") == "1"

# Polite polling cadences (seconds).
WEATHER_POLL_INTERVAL = int(os.environ.get("AETHER_WEATHER_POLL_INTERVAL", "3600"))
EARTHQUAKE_POLL_INTERVAL = int(os.environ.get("AETHER_EARTHQUAKE_POLL_INTERVAL", "300"))
TSUNAMI_POLL_INTERVAL = int(os.environ.get("AETHER_TSUNAMI_POLL_INTERVAL", "300"))


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
