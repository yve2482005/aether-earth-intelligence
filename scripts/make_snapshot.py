#!/usr/bin/env python3
"""AETHER snapshot generator — bundles REAL collected backend data into the APK.

The Android app is a file:// WebView with no reachable backend, so on launch
every /v1 fetch fails. This script snapshots the endpoints the app needs
(default location: Yangon) into app/src/main/assets/www/data/snapshot/.
The app's api.request() falls back to these files (network -> localStorage
cache -> bundled snapshot -> honest empty state), with the staleness banner
showing the snapshot age. No data is invented: only successful (HTTP 200)
responses are written; failures are skipped and the app shows empty states.

Key normalization MUST match app.js:
    path.replace(/[^a-zA-Z0-9]/g, '_').slice(0, 120)
"""
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "web" / "data" / "snapshot"

LAT, LON = 16.84, 96.17
Q = f"?lat={LAT}&lon={LON}"
LOC_ID = 1  # Yangon in the DB (closest to default location)

PATHS = [
    "/health",
    "/locations",
    "/alerts",
    "/earthquakes",
    "/tsunami",
    "/events",
    "/accuracy",
    "/trust/sources",
    "/notify/history?limit=20",
    "/notify/preferences?user_id=local",
    "/notify/travel?user_id=local",
    "/notify/config",
    f"/weather/current{Q}",
    f"/weather/hourly{Q}",
    f"/weather/daily{Q}",
    f"/engines/sunmoon{Q}",
    f"/nowcast{Q}",
    f"/engines/ocean{Q}&hours=48",
    f"/accuracy/models?location_id={LOC_ID}&variable=temperature&horizon=24",
    f"/trust/index?location_id={LOC_ID}",
]


def key(path: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "_", path)[:120]


def main() -> int:
    sys.path.insert(0, str(ROOT))
    from fastapi.testclient import TestClient
    from backend.api.app import app

    OUT.mkdir(parents=True, exist_ok=True)
    client = TestClient(app)
    now = datetime.now(timezone.utc).isoformat()
    ok, skipped = [], []

    def snap(path: str):
        try:
            r = client.get(f"/v1{path}")
        except Exception as e:  # noqa: BLE001
            skipped.append((path, f"exception: {e}"))
            return None
        if r.status_code != 200:
            skipped.append((path, f"HTTP {r.status_code}"))
            return None
        try:
            payload = r.json()
        except Exception as e:  # noqa: BLE001
            skipped.append((path, f"bad json: {e}"))
            return None
        (OUT / f"{key(path)}.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        ok.append(path)
        return payload

    for p in PATHS:
        snap(p)

    # Per-item details for the top recent earthquakes + tsunamis (drill-down views).
    eq = snap("/earthquakes")
    if eq:
        events = eq.get("events") or eq.get("earthquakes") or []
        for ev in events[:5]:
            eid = ev.get("canonical_event_id") or ev.get("id") or ev.get("event_id")
            if not eid:
                continue
            snap(f"/earthquakes/{eid}")
            snap(f"/engines/aftershock?event_id={eid}")
    ts = snap("/tsunami")
    if ts:
        events = ts.get("events") or ts.get("tsunamis") or []
        for ev in events[:3]:
            eid = ev.get("canonical_event_id") or ev.get("id") or ev.get("event_id")
            if eid:
                snap(f"/tsunami/{eid}")

    manifest = {
        "generated_at": now,
        "generator": "scripts/make_snapshot.py",
        "default_location": {"lat": LAT, "lon": LON, "location_id": LOC_ID},
        "keys": sorted(k for k in ok),
        "skipped": [{"path": p, "reason": r} for p, r in skipped],
        "note": "Bundled real backend responses. The app labels these with their age; "
        "they are replaced by live data whenever a backend is reachable.",
    }
    (OUT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"snapshot: {len(ok)} endpoints written, {len(skipped)} skipped -> {OUT}")
    for p, r in skipped:
        print(f"  SKIP {p}: {r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
