"""Shared pytest fixtures for the AETHER ML test suite."""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

WEATHER_DDL = """
CREATE TABLE weather_forecasts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    location_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    model TEXT NOT NULL,
    forecast_run TEXT NOT NULL,
    forecast_hour INTEGER NOT NULL,
    issued_at TEXT NOT NULL,
    valid_time TEXT NOT NULL,
    temperature REAL, feels_like REAL, humidity REAL, pressure REAL,
    wind_speed REAL, wind_direction REAL, precipitation REAL,
    precip_probability REAL, cloud_cover REAL, visibility REAL, uv REAL,
    aqi REAL,
    collected_at TEXT NOT NULL
);
CREATE TABLE weather_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    location_id INTEGER NOT NULL,
    station TEXT,
    observed_at TEXT NOT NULL,
    temperature REAL, humidity REAL, pressure REAL,
    wind_speed REAL, wind_direction REAL, precipitation REAL,
    source TEXT NOT NULL,
    quality TEXT,
    collected_at TEXT NOT NULL
);
CREATE TABLE locations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL, latitude REAL NOT NULL, longitude REAL NOT NULL,
    timezone TEXT, country TEXT, is_coastal INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);
"""


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")




def add_forecast(db: str, valid_time: str, loc: int = 1, model: str = "gfs",
                 fh: int = 0, temp: float = 30.0, precip: float = 0.0,
                 prob: float = 0.1, cloud: float = 20.0):
    conn = sqlite3.connect(db)
    conn.execute(
        """INSERT INTO weather_forecasts
           (location_id, source, model, forecast_run, forecast_hour,
            issued_at, valid_time, temperature, humidity, pressure,
            wind_speed, precipitation, precip_probability, cloud_cover,
            collected_at)
           VALUES (1,'open-meteo',?,?,?,?,?,?,?,?,?,?,?,?,?)""".replace(
            "(1,", f"({loc},", 1),
        (model, valid_time, fh, valid_time, valid_time, temp, 70.0, 1008.0,
         3.0, precip, prob, cloud, valid_time),
    )
    conn.commit()
    conn.close()


def add_observation(db: str, observed_at: str, loc: int = 1, temp: float = 29.5,
                    precip: float = 0.0, quality: str = "ok"):
    conn = sqlite3.connect(db)
    conn.execute(
        """INSERT INTO weather_observations
           (location_id, station, observed_at, temperature, humidity,
            pressure, wind_speed, precipitation, source, quality,
            collected_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (loc, "TEST", observed_at, temp, 72.0, 1007.0, 3.5, precip,
         "open-meteo", quality, observed_at),
    )
    conn.commit()
    conn.close()


