"""Generate a clearly-labeled SYNTHETIC weather fixture for pipeline smoke tests.

Output: ml/fixtures/synthetic_smoke_weather.json
  {"label": "SYNTHETIC SMOKE TEST FIXTURE — NOT REAL DATA", ...}

This data is random-but-plausible (seeded) and exists ONLY so the ML
pipeline (dataset -> train -> calibrate -> evaluate -> backtest) can be
exercised end-to-end before real observations accumulate. It must NEVER be
treated as real data:

  * training_runs.notes for these runs says
    "synthetic smoke test — not a real model"
  * such versions are never deployed and never marked champion
    (enforced in backend/ml_support/registry.py deploy_gate)
  * the scratch DB (default data/synthetic_smoke.db) is separate from the
    real data/aether.db

CLI:
  python -m ml.fixtures.make_synthetic_smoke --json-out PATH --db-out PATH
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

LABEL = "SYNTHETIC SMOKE TEST FIXTURE \u2014 NOT REAL DATA"
SEED = 42
LOCATIONS = [
    {"id": 9001, "name": "Synthetic City A (smoke test)",
     "latitude": 16.8, "longitude": 96.15, "timezone": "Asia/Yangon",
     "country": "MM", "is_coastal": 1},
    {"id": 9002, "name": "Synthetic City B (smoke test)",
     "latitude": 21.95, "longitude": 96.08, "timezone": "Asia/Yangon",
     "country": "MM", "is_coastal": 0},
]
MODELS = ["gfs", "icon_seamless"]
MODEL_TEMP_BIAS = {"gfs": 0.3, "icon_seamless": -0.2}
DAYS = 30
START = datetime(2026, 9, 5, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def generate(seed: int = SEED, days: int = DAYS) -> dict:
    rng = np.random.RandomState(seed)
    forecasts, observations = [], []
    for loc in LOCATIONS:
        # observations: one per hour
        obs_rows = []
        for h in range(days * 24):
            t = START + timedelta(hours=h)
            hod = h % 24
            # latent "true" weather
            rain_regime = rng.rand() < 0.28
            true_temp = 28.5 + 4.0 * np.sin(2 * np.pi * (hod - 9) / 24) \
                + rng.normal(0, 0.6)
            true_precip = (max(0.0, rng.exponential(4.0))
                           if rain_regime else 0.0)
            true_cloud = float(np.clip(
                (70 if rain_regime else 25) + rng.normal(0, 12), 0, 100))
            true_hum = float(np.clip(72 + (18 if rain_regime else -8)
                                     + rng.normal(0, 5), 5, 100))
            true_pres = 1008.0 + rng.normal(0, 2.0)
            true_wind = max(0.0, 3.2 + rng.normal(0, 1.4))
            obs_rows.append({
                "location_id": loc["id"], "station": "SYNTHETIC",
                "observed_at": _iso(t),
                "temperature": round(true_temp, 2),
                "humidity": round(true_hum, 1),
                "pressure": round(true_pres, 1),
                "wind_speed": round(true_wind, 2),
                "precipitation": round(true_precip, 2),
                "source": "synthetic_smoke", "quality": "ok",
                "_true_cloud": true_cloud,
                "_rain_regime": bool(rain_regime),
            })
        observations.extend(obs_rows)

        # forecasts: one run per day per model, hourly steps 0..23
        for model in MODELS:
            bias = MODEL_TEMP_BIAS[model]
            for d in range(days):
                run = START + timedelta(days=d)
                for fh in range(24):
                    vt = run + timedelta(hours=fh)
                    hod = fh
                    # find the matching latent truth
                    o = obs_rows[d * 24 + fh]
                    f_temp = o["temperature"] + bias + rng.normal(0, 0.9)
                    f_prob = float(np.clip(
                        (0.75 if o["_rain_regime"] else 0.08)
                        + rng.normal(0, 0.12), 0, 1))
                    f_precip = max(0.0, o["precipitation"] * rng.normal(1.0, 0.25)
                                   + rng.normal(0, 0.6))
                    f_cloud = float(np.clip(
                        o["_true_cloud"] + rng.normal(0, 10), 0, 100))
                    forecasts.append({
                        "location_id": loc["id"], "source": "synthetic_smoke",
                        "model": model, "forecast_run": _iso(run),
                        "forecast_hour": fh, "issued_at": _iso(run),
                        "valid_time": _iso(vt),
                        "temperature": round(f_temp, 2),
                        "humidity": round(float(np.clip(
                            o["humidity"] + rng.normal(0, 6), 5, 100)), 1),
                        "pressure": round(o["pressure"] + rng.normal(0, 1.2), 1),
                        "wind_speed": round(max(
                            0.0, o["wind_speed"] + rng.normal(0, 1.0)), 2),
                        "precipitation": round(f_precip, 2),
                        "precip_probability": round(f_prob, 3),
                        "cloud_cover": round(f_cloud, 1),
                        "collected_at": _iso(run),
                    })
    for o in observations:
        o.pop("_true_cloud", None)
        o.pop("_rain_regime", None)
    return {
        "label": LABEL,
        "warning": ("SYNTHETIC data for pipeline smoke-testing only. "
                    "Not real observations. Never train a champion on this."),
        "generated_at": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
        "generator": "ml/fixtures/make_synthetic_smoke.py",
        "seed": seed,
        "locations": LOCATIONS,
        "forecasts": forecasts,
        "observations": observations,
    }


def load_into_db(fixture: dict, db_path: str) -> dict:
    """Create schema + insert fixture into a SCRATCH db (not the real one)."""
    schema = (ROOT / "backend" / "db" / "schema.sql").read_text()
    conn = sqlite3.connect(db_path)
    conn.executescript(schema)
    for loc in fixture["locations"]:
        conn.execute(
            """INSERT OR REPLACE INTO locations
               (id, name, latitude, longitude, timezone, country, is_coastal,
                created_at) VALUES (?,?,?,?,?,?,?,?)""",
            (loc["id"], loc["name"], loc["latitude"], loc["longitude"],
             loc["timezone"], loc["country"], loc["is_coastal"],
             fixture["generated_at"]))
    for f in fixture["forecasts"]:
        conn.execute(
            """INSERT OR IGNORE INTO weather_forecasts
               (location_id, source, model, forecast_run, forecast_hour,
                issued_at, valid_time, temperature, humidity, pressure,
                wind_speed, precipitation, precip_probability, cloud_cover,
                collected_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (f["location_id"], f["source"], f["model"], f["forecast_run"],
             f["forecast_hour"], f["issued_at"], f["valid_time"],
             f["temperature"], f["humidity"], f["pressure"], f["wind_speed"],
             f["precipitation"], f["precip_probability"], f["cloud_cover"],
             f["collected_at"]))
    for o in fixture["observations"]:
        conn.execute(
            """INSERT OR IGNORE INTO weather_observations
               (location_id, station, observed_at, temperature, humidity,
                pressure, wind_speed, precipitation, source, quality,
                collected_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (o["location_id"], o["station"], o["observed_at"],
             o["temperature"], o["humidity"], o["pressure"], o["wind_speed"],
             o["precipitation"], o["source"], o["quality"],
             fixture["generated_at"]))
    conn.commit()
    counts = {
        t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ("locations", "weather_forecasts", "weather_observations")
    }
    conn.close()
    return counts


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json-out",
                    default=str(ROOT / "ml" / "fixtures"
                                / "synthetic_smoke_weather.json"))
    ap.add_argument("--db-out",
                    default=str(ROOT / "data" / "synthetic_smoke.db"))
    args = ap.parse_args(argv)
    fixture = generate()
    Path(args.json_out).write_text(json.dumps(fixture, indent=1))
    Path(args.db_out).unlink(missing_ok=True)
    counts = load_into_db(fixture, args.db_out)
    print(json.dumps({"label": LABEL, "json": args.json_out,
                      "db": args.db_out, "counts": counts}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
