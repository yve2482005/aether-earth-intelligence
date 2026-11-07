"""HTTP helper: requests Session with retry + exponential backoff + timeout.

Every call returns a dict with health info:
  {ok, status_code, latency_ms, data, error, url, attempts}
Never raises for HTTP errors; only unexpected exceptions are logged, not raised.
"""
from __future__ import annotations

import time
from typing import Any, Dict, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import HTTP_BACKOFF, HTTP_RETRIES, HTTP_TIMEOUT
from .logging_setup import setup_logging

log = setup_logging("aether.http")


def make_session(retries: int = HTTP_RETRIES, backoff: float = HTTP_BACKOFF,
                 timeout: float = HTTP_TIMEOUT) -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=retries,
        backoff_factor=backoff,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "HEAD"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    s.headers.update({"User-Agent": "AETHER-Earth-Intelligence/1.0 (+non-commercial research use)"})
    s.request_timeout = timeout  # informational; we pass timeout per call
    return s


def fetch_json(session: requests.Session, url: str, params: Optional[Dict[str, Any]] = None,
               timeout: float = HTTP_TIMEOUT) -> Dict[str, Any]:
    """GET a URL, parse JSON, return health info dict. Retries handled by adapter + manual backoff."""
    t0 = time.monotonic()
    attempts = 0
    last_error: Optional[str] = None
    for attempt in range(HTTP_RETRIES + 1):
        attempts = attempt + 1
        try:
            resp = session.get(url, params=params, timeout=timeout)
            latency_ms = (time.monotonic() - t0) * 1000.0
            if resp.status_code == 200:
                try:
                    return {"ok": True, "status_code": 200, "latency_ms": latency_ms,
                            "data": resp.json(), "error": None, "url": resp.url, "attempts": attempts}
                except ValueError as e:
                    return {"ok": False, "status_code": 200, "latency_ms": latency_ms,
                            "data": None, "error": f"invalid JSON: {e}", "url": resp.url, "attempts": attempts}
            last_error = f"HTTP {resp.status_code}"
            if resp.status_code in (429, 500, 502, 503, 504):
                time.sleep(HTTP_BACKOFF * (2 ** attempt))
                continue
            return {"ok": False, "status_code": resp.status_code, "latency_ms": latency_ms,
                    "data": None, "error": last_error, "url": url, "attempts": attempts}
        except requests.RequestException as e:
            last_error = f"{type(e).__name__}: {e}"
            log.warning("fetch %s failed (attempt %d/%d): %s", url, attempts, HTTP_RETRIES + 1, last_error)
            if attempt < HTTP_RETRIES:
                time.sleep(HTTP_BACKOFF * (2 ** attempt))
    latency_ms = (time.monotonic() - t0) * 1000.0
    return {"ok": False, "status_code": None, "latency_ms": latency_ms,
            "data": None, "error": last_error, "url": url, "attempts": attempts}
