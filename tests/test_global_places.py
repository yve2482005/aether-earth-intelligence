"""Global places registry + bulk collector tests (no live network).

Covers: GeoNames TSV parsing into global_places (exact rows/counts),
idempotent reload, batch splitting, multi-coordinate response parsing on
a payload recorded from the real API (2026-10-17, shape-verbatim), the
ingest validation gate, 429 stop behaviour, and checkpoint resume.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.collectors.weather import global_places as gp  # noqa: E402

# --- Fixtures ---------------------------------------------------------------
# Real rows from the GeoNames cities500 dump (19 tab-separated columns).
CITIES_TSV = [
    "3038832\tVila\tVila\tCasas Vila,Vila\t42.53176\t1.56654\tP\tPPL\tAD\t\t03\t\t\t\t1418\t\t1318\tEurope/Andorra\t2024-11-04",
    "3038999\tSoldeu\tSoldeu\t\t42.57688\t1.66769\tP\tPPL\tAD\t\t02\t\t\t\t602\t1832\t1832\tEurope/Andorra\t2012-01-19",
    "1298824\tYangon\tYangon\tRangoon\t16.80528\t96.15611\tP\tPPLA\tMM\t\t14\t\t\t\t4477638\t13\t18\tAsia/Yangon\t2023-09-02",
    "badrow\tonly-three\tfields",
    "notanint\tNowhere\tNowhere\t\t0.0\t0.0\tP\tPPL\tXX\t\t\t\t\t\t100\t\t\tUTC\t2020-01-01",
]
COUNTRY_TSV = [
    "# comment line",
    "AD\tAND\t020\tAN\tAndorra\tAndorra la Vella\t468\t82904\tEU",
    "MM\tMMR\t104\tBM\tMyanmar\tNaypyidaw\t676578\t54409800\tAS",
]

# Recorded from a live Open-Meteo multi-coordinate call, 2026-10-17
# (values verbatim; Yangon/Bangkok/London coordinates requested).
RECORDED_PAYLOAD = [
    {"latitude": 16.836554, "longitude": 96.11484, "timezone": "GMT",
     "current": {"time": "2026-10-17T15:30", "interval": 900,
                 "temperature_2m": 26.1, "relative_humidity_2m": 96,
                 "pressure_msl": 1012.9, "wind_speed_10m": 0.65,
                 "precipitation": 0.1, "weather_code": 51}},
    {"latitude": 13.75, "longitude": 100.5, "timezone": "GMT",
     "current": {"time": "2026-10-17T15:30", "interval": 900,
                 "temperature_2m": 31.4, "relative_humidity_2m": 71,
                 "pressure_msl": 1009.2, "wind_speed_10m": 3.1,
                 "precipitation": 0.0, "weather_code": 2}},
    {"latitude": 51.5, "longitude": -0.12, "timezone": "GMT",
     "current": {"time": "2026-10-17T15:45", "interval": 900,
                 "temperature_2m": 12.8, "relative_humidity_2m": 82,
                 "pressure_msl": 1001.5, "wind_speed_10m": 5.4,
                 "precipitation": 0.4, "weather_code": 61}},
]
RECORDED_BATCH = [
    {"geoname_id": 1298824, "latitude": 16.80, "longitude": 96.15},
    {"geoname_id": 1609350, "latitude": 13.75, "longitude": 100.50},
    {"geoname_id": 2643743, "latitude": 51.50, "longitude": -0.12},
]


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "g.db"))
    yield c
    c.close()


def _load_fixture(conn):
    return gp.load_registry(conn, CITIES_TSV, COUNTRY_TSV)


# --- Registry loader --------------------------------------------------------

def test_loader_parses_fixture_exactly(conn):
    out = _load_fixture(conn)
    # 3 valid rows (badrow + notanint are skipped, never guessed at)
    assert out == {"parsed": 3, "places": 3, "countries": 2}
    rows = conn.execute(
        "SELECT geoname_id, name, ascii_name, country_code, country_name,"
        " latitude, longitude, population, timezone, elevation"
        " FROM global_places ORDER BY geoname_id").fetchall()
    assert rows[0] == (1298824, "Yangon", "Yangon", "MM", "Myanmar",
                       16.80528, 96.15611, 4477638, "Asia/Yangon", 13.0)
    # elevation fell back to the DEM column (col 15 empty, col 16 = 1318)
    assert rows[1][0] == 3038832 and rows[1][9] == 1318.0
    assert rows[1][4] == "Andorra"  # country name resolved from countryInfo
    # explicit elevation column wins when present
    assert rows[2][0] == 3038999 and rows[2][9] == 1832.0


def test_loader_idempotent(conn):
    _load_fixture(conn)
    out = _load_fixture(conn)
    assert out["places"] == 3
    assert conn.execute("SELECT COUNT(*) FROM global_places").fetchone()[0] == 3


# --- Batching + response parsing ---------------------------------------------

def test_split_batches():
    assert gp.split_batches(list(range(10)), 3) == [
        [0, 1, 2], [3, 4, 5], [6, 7, 8], [9]]
    assert gp.split_batches([], 5) == []
    with pytest.raises(ValueError):
        gp.split_batches([1], 0)


def test_build_params_multi_coordinate():
    p = gp.build_params(RECORDED_BATCH[:2])
    assert p["latitude"] == "16.8,13.75"
    assert p["longitude"] == "96.15,100.5"
    assert p["wind_speed_unit"] == "ms"
    assert "temperature_2m" in p["current"]


def test_parse_recorded_payload():
    readings = gp.parse_bulk_response(RECORDED_PAYLOAD, RECORDED_BATCH)
    assert len(readings) == 3
    assert readings[0] == {
        "geoname_id": 1298824, "observed_at": "2026-10-17T15:30:00Z",
        "temperature": 26.1, "humidity": 96, "pressure": 1012.9,
        "wind_speed": 0.65, "precipitation": 0.1, "weather_code": 51}
    assert readings[2]["observed_at"] == "2026-10-17T15:45:00Z"


def test_parse_drops_mispaired_entry():
    # Second entry's coordinates are >1 degree from the requested place:
    # pairing must be dropped, never misattributed to Bangkok.
    payload = [dict(RECORDED_PAYLOAD[0]),
               dict(RECORDED_PAYLOAD[1], latitude=-33.86, longitude=151.21),
               dict(RECORDED_PAYLOAD[2])]
    readings = gp.parse_bulk_response(payload, RECORDED_BATCH)
    assert [r["geoname_id"] for r in readings] == [1298824, 2643743]


def test_parse_single_object_shape():
    readings = gp.parse_bulk_response(RECORDED_PAYLOAD[0], RECORDED_BATCH[:1])
    assert len(readings) == 1 and readings[0]["temperature"] == 26.1


# --- Validation gate ----------------------------------------------------------

def test_reading_in_range_boundaries():
    base = {"temperature": 20.0, "humidity": 50, "pressure": 1013.0,
            "wind_speed": 2.0, "precipitation": 0.0}
    assert gp.reading_in_range(base)
    assert gp.reading_in_range(dict(base, temperature=None))  # NULL passes
    for field, bad in (("temperature", 60.1), ("temperature", -90.1),
                       ("humidity", 100.1), ("humidity", -0.1),
                       ("pressure", 799.9), ("pressure", 1100.1),
                       ("wind_speed", -0.1), ("precipitation", -0.1),
                       ("temperature", float("nan"))):
        assert not gp.reading_in_range(dict(base, **{field: bad})), (field, bad)


# --- Sweep: checkpoint / resume / 429 -----------------------------------------

def _fake_payload(params):
    lats = params["latitude"].split(",")
    lons = params["longitude"].split(",")
    return {"ok": True, "status_code": 200, "error": None, "data": [
        {"latitude": float(la), "longitude": float(lo),
         "current": {"time": "2026-10-17T15:30", "temperature_2m": 25.0,
                     "relative_humidity_2m": 60, "pressure_msl": 1010.0,
                     "wind_speed_10m": 2.0, "precipitation": 0.0,
                     "weather_code": 1}}
        for la, lo in zip(lats, lons)]}


def _seed_places(conn, n=250):
    gp.ensure_tables(conn)
    conn.executemany(
        "INSERT INTO global_places (geoname_id, name, latitude, longitude)"
        " VALUES (?,?,?,?)",
        [(1000 + i, f"Place{i}", 10.0 + i * 0.01, 20.0) for i in range(n)])
    conn.commit()


def test_sweep_checkpoint_resume(conn, monkeypatch):
    _seed_places(conn, 250)
    calls = []

    def counting_fetch(session, url, params=None, timeout=None):
        calls.append(params)
        return _fake_payload(params)

    monkeypatch.setattr(gp, "fetch_json", counting_fetch)

    s1 = gp.run_sweep(conn, session=None, batch_size=100, max_batches=1)
    assert s1["status"] == "partial" and s1["completed_batches"] == 1
    assert s1["rows_stored"] == 100 and len(calls) == 1
    state = gp.load_sweep_state(conn)
    assert state["completed"] == [0]
    assert gp.pending_batches(state) == [1, 2]

    s2 = gp.run_sweep(conn, session=None, batch_size=100)  # resume
    assert s2["status"] == "complete" and s2["completed_batches"] == 3
    assert len(calls) == 3  # only the two pending batches were fetched
    assert conn.execute(
        "SELECT COUNT(*) FROM global_place_weather").fetchone()[0] == 250

    # Third run over a completed sweep starts a new sweep but INSERT OR
    # IGNORE means no duplicate snapshots appear.
    gp.run_sweep(conn, session=None, batch_size=100)
    assert conn.execute(
        "SELECT COUNT(*) FROM global_place_weather").fetchone()[0] == 250


def test_sweep_stops_on_429(conn, monkeypatch):
    _seed_places(conn, 250)

    def limited(session, url, params=None, timeout=None):
        # The shape fetch_json actually returns for an exhausted 429:
        # status_code None, the status carried in the error string.
        return {"ok": False, "status_code": None, "error": "HTTP 429",
                "data": None}

    monkeypatch.setattr(gp, "fetch_json", limited)
    s = gp.run_sweep(conn, session=None, batch_size=100)
    assert s["status"] == "stopped_429" and s["stop_reason"] == "http_429"
    assert s["completed_batches"] == 0
    assert gp.load_sweep_state(conn)["completed"] == []  # checkpoint intact


def test_sweep_rejects_out_of_range(conn, monkeypatch):
    _seed_places(conn, 3)

    def dirty(session, url, params=None, timeout=None):
        out = _fake_payload(params)
        out["data"][1]["current"]["temperature_2m"] = 999.0  # gate violation
        return out

    monkeypatch.setattr(gp, "fetch_json", dirty)
    s = gp.run_sweep(conn, session=None, batch_size=100)
    assert s["status"] == "complete"
    assert s["rows_stored"] == 2 and s["rows_rejected"] == 1


def test_write_with_retry_recovers_from_lock():
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise sqlite3.OperationalError("database is locked")
        return "ok"

    assert gp._write_with_retry(flaky, attempts=5, delay_s=0) == "ok"
    assert attempts["n"] == 3

    def other_error():
        raise sqlite3.OperationalError("no such table: x")

    with pytest.raises(sqlite3.OperationalError):
        gp._write_with_retry(other_error, attempts=5, delay_s=0)
