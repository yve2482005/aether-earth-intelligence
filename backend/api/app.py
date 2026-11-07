"""AETHER Earth Intelligence API (FastAPI, base /v1).

Plus: /health, /status, /metrics (Prometheus-style text).
CORS enabled. Every user-visible payload includes provenance and kind tags
(see routes).
"""
from __future__ import annotations

import importlib

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse

from backend.api import middleware as api_middleware
from backend.api._util import provenance
from backend.api.routes import (accuracy, alerts, earthquakes, events, health,
                                locations, models, tsunami, weather)
from backend.common.logging_setup import setup_logging
from backend.common.timeutil import utc_iso
from backend.db import db as dbmod

log = setup_logging("aether.api")

app = FastAPI(title="AETHER Earth Intelligence API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- v2 platform middleware (workstream E) -----------------------------------
# Rate limiting (token bucket, AETHER_RATE_LIMIT_RPM/min/IP on /v1/*) and
# per-request logging. API-key auth (backend/api/auth.py) is opt-in per route
# via Depends(require_api_key) and is NOT registered globally here.
app.add_middleware(api_middleware.RequestLoggingMiddleware)
app.add_middleware(api_middleware.RateLimitMiddleware,
                   requests_per_minute=api_middleware.RATE_LIMIT_RPM,
                   burst=api_middleware.RATE_LIMIT_BURST)

for r in (weather.router, earthquakes.router, tsunami.router, alerts.router,
          events.router, accuracy.router, models.router, locations.router, health.router):
    app.include_router(r)

# --- v2 route modules (workstream E) ------------------------------------------
# Modules landed by other v2 workstreams (B, C, D); each exposes
# `router = APIRouter()`. A module that has not landed yet is skipped so
# parallel work never breaks the app. Any other import-time error still
# propagates so real bugs surface loudly.
_V2_ROUTE_MODULES = ("nowcast", "engines", "notify", "trust", "admin", "replay", "perf", "datamgmt", "global_weather")
_v2_loaded = []
for _mod_name in _V2_ROUTE_MODULES:
    try:
        _mod = importlib.import_module("backend.api.routes." + _mod_name)
    except ImportError as exc:
        log.debug("v2 route module %s not present yet (%s); skipping", _mod_name, exc)
        continue
    _router = getattr(_mod, "router", None)
    if _router is None:
        log.warning("v2 route module %s has no `router`; skipping", _mod_name)
        continue
    app.include_router(_router)
    _v2_loaded.append(_mod_name)
del _mod_name
log.info("v2 route modules loaded: %s",
         ", ".join(_v2_loaded) if _v2_loaded else "none yet")


@app.get("/health")
def health_root():
    return {"status": "ok", "service": "aether-api", "time": utc_iso()}


@app.get("/status")
def status():
    conn = dbmod.connect()
    try:
        tables = {}
        for t in ("weather_forecasts", "weather_observations", "earthquake_events",
                  "earthquake_updates", "tsunami_events", "tsunami_messages",
                  "earth_events", "alerts", "model_scores", "model_versions"):
            tables[t] = dbmod.table_count(conn, t)
        latest = dbmod.query_one(conn, "SELECT MAX(checked_at) AS t FROM system_health")
        return {"service": "aether-api", "time": utc_iso(), "tables": tables,
                "provenance": provenance(["aether-api"], latest["t"] if latest else None)}
    finally:
        conn.close()


@app.get("/metrics", response_class=PlainTextResponse)
def metrics():
    conn = dbmod.connect()
    try:
        lines = ["# HELP aether_table_rows Rows per AETHER table",
                 "# TYPE aether_table_rows gauge"]
        for t in ("weather_forecasts", "weather_observations", "earthquake_events",
                  "earthquake_updates", "tsunami_events", "tsunami_messages",
                  "alerts", "earth_events"):
            lines.append(f'aether_table_rows{{table="{t}"}} {dbmod.table_count(conn, t)}')
        lines += ["# HELP aether_collector_status Last collector status (1=ok,0=not ok)",
                  "# TYPE aether_collector_status gauge"]
        for r in dbmod.query(conn, """SELECT component, status FROM system_health
                WHERE checked_at = (SELECT MAX(checked_at) FROM system_health s2
                                    WHERE s2.component = system_health.component)"""):
            v = 1 if r["status"] == "ok" else 0
            lines.append(f'aether_collector_status{{component="{r["component"]}"}} {v}')
        return "\n".join(lines) + "\n"
    finally:
        conn.close()
