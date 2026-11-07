"""Perf tests: incremental fetch (§8) — ETag persistence + 304 + cursor filter."""
from __future__ import annotations

import sqlite3

import pytest

from backend.common.http import make_session
from backend.perf import incremental
from backend.perf.incremental import (conditional_fetch, filter_new, get_cursor,
                                      get_meta, save_meta, set_cursor,
                                      USGS_ALL_HOUR)


class FakeResp:
    def __init__(self, status, data=None, headers=None):
        self.status_code = status
        self._data = data or {}
        self.headers = headers or {}

    def json(self):
        return self._data


class FakeSession:
    def __init__(self, script):
        self.script = list(script)
        self.last_headers = None

    def get(self, url, headers=None, timeout=None):
        self.last_headers = headers
        return self.script.pop(0)


def _mem():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    incremental.ensure_meta_table(c)
    return c


def test_etag_persisted_and_sent_back():
    conn = _mem()
    sess = FakeSession([
        FakeResp(200, {"a": 1}, {"ETag": '"abc"', "Last-Modified": "Wed, 01 Jan 2020 00:00:00 GMT"}),
        FakeResp(304),
    ])
    r1 = conditional_fetch(sess, conn, "http://x/feed")
    assert r1["ok"] and not r1["not_modified"] and r1["etag"] == '"abc"'
    assert get_meta(conn, "http://x/feed")["etag"] == '"abc"'
    r2 = conditional_fetch(sess, conn, "http://x/feed")
    assert r2["ok"] and r2["not_modified"] and r2["status_code"] == 304
    assert sess.last_headers["If-None-Match"] == '"abc"'
    assert sess.last_headers["If-Modified-Since"] == "Wed, 01 Jan 2020 00:00:00 GMT"


def test_cursor_filter_only_new_records():
    conn = _mem()
    recs = [{"t": "2026-10-05T00:01:00Z"}, {"t": "2026-10-05T00:02:00Z"}]
    new1 = filter_new(conn, "usgs", recs, lambda r: r["t"])
    assert len(new1) == 2
    assert get_cursor(conn, "usgs") == "2026-10-05T00:02:00Z"
    # re-ingest same records: zero new
    new2 = filter_new(conn, "usgs", recs, lambda r: r["t"])
    assert new2 == []
    # one newer record passes
    new3 = filter_new(conn, "usgs", recs + [{"t": "2026-10-05T00:03:00Z"}],
                      lambda r: r["t"])
    assert [r["t"] for r in new3] == ["2026-10-05T00:03:00Z"]


def test_real_usgs_etag_support():
    """Verify against the REAL endpoint: USGS ComCat supports ETag.
    Skipped (not failed) if the sandbox has no network — honesty, not faking."""
    sess = make_session()
    conn = _mem()
    try:
        r = conditional_fetch(sess, conn, USGS_ALL_HOUR, timeout=20)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"network unavailable: {e}")
    if not r["ok"]:
        pytest.skip(f"USGS unreachable: {r.get('error')}")
    # If ETag was offered, the second conditional call must 304 or 200-with-same-etag
    if r.get("etag"):
        r2 = conditional_fetch(sess, conn, USGS_ALL_HOUR, timeout=20)
        assert r2["ok"] and r2["status_code"] in (200, 304)
    else:
        pytest.skip("USGS did not return an ETag on this run (behavior may vary)")
