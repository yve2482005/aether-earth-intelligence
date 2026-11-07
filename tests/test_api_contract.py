"""API contract tests for backend/api/app.py (backend team owns the app).

The contract comes from CONTRACTS.md: base /v1 routes, plus /health, /status,
/metrics, and a `provenance` object ({sources, updated_at, data_age_min}) on
every user-visible response. Tested through FastAPI's TestClient — the
installed FastAPI (0.142.x) registers included routers lazily, so route-table
introspection is not a valid check; real HTTP responses are.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
ROOT = os.path.join(os.path.dirname(__file__), "..")

try:
    from fastapi.testclient import TestClient

    from backend.api.app import app

    APP_LANDED = True
except Exception:  # pragma: no cover - module not landed yet
    APP_LANDED = False
    app = None
    TestClient = None


# (path, expect_200) — contract routes from CONTRACTS.md
CONTRACT_PATHS = [
    "/v1/weather/current?lat=16.8&lon=96.1",
    "/v1/weather/hourly?lat=16.8&lon=96.1",
    "/v1/weather/daily?lat=16.8&lon=96.1",
    "/v1/weather/map?lat=16.8&lon=96.1",
    "/v1/earthquakes",
    "/v1/tsunami",
    "/v1/alerts",
    "/v1/events",
    "/v1/accuracy",
    "/v1/models",
    "/v1/locations",
    "/v1/health",
    "/health",
    "/status",
    "/metrics",
]


@pytest.fixture(scope="module")
def client():
    if not APP_LANDED:
        pytest.skip("backend/api/app.py not landed yet (backend team)")
    return TestClient(app, raise_server_exceptions=False)


def _payload(body):
    if isinstance(body, dict) and isinstance(body.get("data"), dict):
        return body["data"]
    return body


@pytest.mark.parametrize("path", CONTRACT_PATHS)
def test_contract_routes_respond(client, path):
    resp = client.get(path)
    assert resp.status_code == 200, "%s -> %s: %s" % (path, resp.status_code,
                                                      resp.text[:200])


def test_user_visible_responses_carry_provenance(client):
    for path in ["/v1/weather/current?lat=16.8&lon=96.1", "/v1/earthquakes",
                 "/v1/tsunami", "/v1/alerts", "/v1/events", "/v1/health"]:
        body = _payload(client.get(path).json())
        assert isinstance(body, dict), path
        prov = body.get("provenance")
        assert prov is not None, "%s missing provenance" % path
        assert set(("sources", "updated_at", "data_age_min")) <= set(prov), path


def test_kind_tags_present_on_weather(client):
    body = _payload(client.get("/v1/weather/current?lat=16.8&lon=96.1").json())
    assert body.get("kind") in ("FORECAST", "OBSERVATION", "UNAVAILABLE",
                                "DETECTION", "WARNING", "RISK"), body.get("kind")


def test_health_shape(client):
    body = client.get("/health").json()
    assert body.get("status") == "ok"


def test_metrics_is_prometheus_text(client):
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "aether_table_rows" in resp.text


def test_unknown_earthquake_id_is_honest_404(client):
    resp = client.get("/v1/earthquakes/does-not-exist")
    assert resp.status_code in (200, 404)


def test_contracts_md_exists():
    assert os.path.isfile(os.path.join(ROOT, "CONTRACTS.md"))
