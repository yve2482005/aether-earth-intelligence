#!/usr/bin/env python3
"""Load the free GeoNames cities500 registry into global_places (idempotent).

User order 2026-10-17: raw data for 200,000 cities. The registry is the
place index for the bulk collector (scripts/collect_global_places.py);
it is separate from the curated `locations` table owned by the deep
tiered collectors.

Source: https://download.geonames.org/export/dump/cities500.zip
(populated places, population >= 500; CC-BY, no key, zero spend) plus
countryInfo.txt for country names. Downloads are cached under
data/geonames/ and re-used on re-runs (--download forces a refresh).
"""
import argparse
import io
import json
import sys
import urllib.request
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from backend.collectors.weather import global_places as gp  # noqa: E402
from backend.common.config import DATA_DIR  # noqa: E402
from backend.db import db as dbmod  # noqa: E402

CACHE_DIR = DATA_DIR / "geonames"
CITIES_ZIP_URL = "https://download.geonames.org/export/dump/cities500.zip"
COUNTRY_URL = "https://download.geonames.org/export/dump/countryInfo.txt"
UA = {"User-Agent": "AETHER-Earth-Intelligence/1.0 (+non-commercial research use)"}


def _fetch(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=300) as resp, open(dest, "wb") as fh:
        fh.write(resp.read())


def ensure_sources(force: bool = False) -> tuple[Path, Path]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cities_txt = CACHE_DIR / "cities500.txt"
    country_txt = CACHE_DIR / "countryInfo.txt"
    if force or not cities_txt.exists():
        zip_path = CACHE_DIR / "cities500.zip"
        _fetch(CITIES_ZIP_URL, zip_path)
        with zipfile.ZipFile(zip_path) as zf:
            with zf.open("cities500.txt") as src, open(cities_txt, "wb") as dst:
                dst.write(src.read())
    if force or not country_txt.exists():
        _fetch(COUNTRY_URL, country_txt)
    return cities_txt, country_txt


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--download", action="store_true",
                    help="force a fresh download of the GeoNames dumps")
    ap.add_argument("--db", default=None, help="DB path (default: project config)")
    args = ap.parse_args()

    cities_txt, country_txt = ensure_sources(force=args.download)
    conn = dbmod.connect(args.db) if args.db else dbmod.connect()
    try:
        conn.execute("PRAGMA busy_timeout=60000")
        with io.open(cities_txt, encoding="utf-8") as fc, \
                io.open(country_txt, encoding="utf-8") as fk:
            out = gp.load_registry(conn, fc, fk)
    finally:
        conn.close()
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
