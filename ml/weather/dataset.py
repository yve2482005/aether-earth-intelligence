"""Forecast<->observation pair dataset for weather post-processing models.

Builds supervised pairs from ``weather_forecasts JOIN weather_observations``
aligned on (location_id, valid_time ~= observed_at) with a 90-minute
tolerance, then applies the leakage guards from :mod:`ml.weather.leakage`.

Features (numpy float array, NaN allowed — HistGradientBoosting handles NaN):
  * raw model outputs: temperature, humidity, pressure, wind_speed,
    precipitation, cloud_cover
  * forecast_hour (lead time), hour_of_day (UTC, from valid_time)
  * model one-hot: gfs | icon_seamless | ecmwf_ifs | jma_seamless
  * regime one-hot (see REGIMES)

Weather regimes are derived from forecast buckets. The schema carries no
weather_code/lightning field, so these are documented approximations from
cloud_cover / precipitation / precip_probability — NOT official
classifications:
  * thunderstorm:        cloud>=85 and prob>=0.8 and precip>=5 mm
  * heavy_rain:          precip>=10 mm
  * monsoon:             precip>=2 mm and prob>=0.5 and cloud>=60
  * tropical_convection: cloud>=40 and (prob>=0.3 or precip>0)
  * clear:               otherwise

Targets per task (NaN where the verifying observation is missing):
  * temperature:      observed temperature (degC)
  * rain_prob: 1 if observed precipitation >= 0.5 mm else 0
  * precip_amount:    observed precipitation (mm)
  * wind:             observed wind speed (m/s)
  * pressure:         observed pressure (hPa)
  * humidity:        observed relative humidity (%)

dataset_version = sha256(params + row count + time range), so any change in
query, data volume, or period produces a new version string.
"""

from __future__ import annotations

import hashlib
import json
import resource
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np

from .leakage import pair_is_valid, validate_feature_names

TASKS = (
    "temperature",
    "rain_prob",
    "precip_amount",
    "wind",
    "pressure",
    "humidity",
)
TASK_COLUMNS = {
    "temperature": "o_temperature",
    "rain_prob": "o_precipitation",
    "precip_amount": "o_precipitation",
    "wind": "o_wind_speed",
    "pressure": "o_pressure",
    "humidity": "o_humidity",
}
RAIN_THRESHOLD_MM = 0.5  # "wet hour" definition for the rain_prob label
MODELS = ("gfs", "icon_seamless", "ecmwf_ifs", "jma_seamless")
REGIMES = (
    "clear",
    "tropical_convection",
    "monsoon",
    "heavy_rain",
    "thunderstorm",
)
# Regime name -> compact int8 code (index into REGIMES). Stored per pair
# as an int8 array instead of a per-pair Python str: at ~6M pairs the str
# list alone costs ~0.3 GB.
REGIME_CODE = {name: i for i, name in enumerate(REGIMES)}


def iso_to_epoch(iso: str) -> int:
    """'YYYY-MM-DDTHH:MM:SSZ' -> UTC epoch seconds (int).

    Fast path slices the fixed-width fields; falls back to fromisoformat
    for any unexpected format.
    """
    try:
        return int(datetime(int(iso[0:4]), int(iso[5:7]), int(iso[8:10]),
                           int(iso[11:13]), int(iso[14:16]), int(iso[17:19]),
                           tzinfo=timezone.utc).timestamp())
    except (ValueError, IndexError, TypeError):
        return int(datetime.fromisoformat(
            iso.replace("Z", "+00:00")).timestamp())


def epoch_to_iso(epoch: int) -> str:
    """UTC epoch seconds (int) -> 'YYYY-MM-DDTHH:MM:SSZ'."""
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def regime_names(codes) -> list[str]:
    """Regime int8 codes -> regime name strings (for calibration, scoring,
    CSV/Sheets export — those contracts take names, not codes)."""
    return [REGIMES[int(c)] for c in codes]


def valid_times_iso(epochs) -> list[str]:
    """Epoch-second valid_times -> ISO 'YYYY-MM-DDTHH:MM:SSZ' strings."""
    return [epoch_to_iso(int(e)) for e in epochs]


def _rss_mb() -> float:
    """Current process peak RSS in MiB (Linux ru_maxrss is KiB)."""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0

BASE_FEATURES = [
    "f_temperature",
    "f_humidity",
    "f_pressure",
    "f_wind_speed",
    "f_precipitation",
    "f_cloud_cover",
    "forecast_hour",
    "hour_of_day",
]


def feature_names() -> list[str]:
    names = list(BASE_FEATURES)
    # model one-hot; unseen model ids fall into model_other (never silent)
    names += [f"model_{m}" for m in MODELS] + ["model_other"]
    names += [f"regime_{r}" for r in REGIMES]
    return names


def classify_regime(cloud: float | None, precip: float | None,
                    prob: float | None) -> str:
    cloud = cloud if cloud is not None else 0.0
    precip = precip if precip is not None else 0.0
    prob = prob if prob is not None else 0.0
    if cloud >= 85 and prob >= 0.8 and precip >= 5.0:
        return "thunderstorm"
    if precip >= 10.0:
        return "heavy_rain"
    if precip >= 2.0 and prob >= 0.5 and cloud >= 60:
        return "monsoon"
    if cloud >= 40 and (prob >= 0.3 or precip > 0.0):
        return "tropical_convection"
    return "clear"


@dataclass
class WeatherDataset:
    X: np.ndarray
    feature_names: list[str]
    y: dict[str, np.ndarray]
    # Per-pair metadata as compact numpy arrays (NOT Python str lists —
    # at ~6M pairs the str lists alone cost ~0.3-0.6 GB):
    #   regimes: int8 codes, index into REGIMES (see regime_names())
    #   valid_times: int64 UTC epoch seconds (see epoch_to_iso())
    #   location_ids: int64
    regimes: np.ndarray
    valid_times: np.ndarray
    location_ids: np.ndarray
    forecast_hours: np.ndarray
    dataset_version: str
    params: dict = field(default_factory=dict)
    n_pairs: int = 0
    time_min: str = ""
    time_max: str = ""


def _fnum(v) -> float:
    return float(v) if v is not None else float("nan")


def _hour_of_day(valid_time: str) -> float:
    """Hour of day (UTC) from an ISO 'YYYY-MM-DDTHH:MM:SSZ' timestamp.

    Fast path slices the fixed-width fields; falls back to fromisoformat
    for any unexpected format so behaviour stays identical to the old
    per-row datetime parsing.
    """
    try:
        if valid_time[10] == "T":
            hour = int(valid_time[11:13])
            minute = int(valid_time[14:16])
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return hour + minute / 60.0
    except (ValueError, IndexError, TypeError):
        pass
    try:
        ts = valid_time.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
        return float(dt.hour) + float(dt.minute) / 60.0
    except ValueError:
        return float("nan")


def build_dataset(db_path: str, location_id: int | None = None,
                  start: str | None = None, end: str | None = None,
                  tolerance_min: float = 90.0) -> WeatherDataset:
    """Build forecast->observation pairs. Raises if leakage guards trip.

    Streams the SQL join straight into preallocated float32 arrays with
    geometric growth. The old two-phase build first accumulated every
    pair as a Python tuple (~500-700 bytes/pair, ~3.5 GB at ~6M pairs)
    and only then filled the arrays, so the tuple list + arrays + lists
    were all alive at once (~5.1 GB RSS) and the OOM killer took the
    trainer down twice on the full worldwide dataset (2026-11-06).
    Per-pair metadata is stored as compact numpy arrays, not Python str
    lists (valid_times: int64 epoch seconds, regimes: int8 codes,
    location_ids: int64) — the str lists alone cost ~0.3-0.6 GB at 6M
    pairs. Progress (rows/cap/RSS) is printed unbuffered every 60 s so
    a future OOM/timeout death leaves a trail.
    Row order, features, targets, leakage guards and the
    dataset_version digest are identical to the old build.
    """
    names = feature_names()
    validate_feature_names(names)
    n_feat = len(names)
    tol_sec = int(tolerance_min * 60)
    q = """
    SELECT f.id AS fid, f.location_id, f.model, f.forecast_hour, f.valid_time,
           f.temperature AS f_temperature, f.humidity AS f_humidity,
           f.pressure AS f_pressure, f.wind_speed AS f_wind_speed,
           f.precipitation AS f_precipitation,
           f.precip_probability AS f_precip_probability,
           f.cloud_cover AS f_cloud_cover,
           o.temperature AS o_temperature, o.humidity AS o_humidity,
           o.pressure AS o_pressure, o.wind_speed AS o_wind_speed,
           o.precipitation AS o_precipitation,
           o.observed_at,
           ABS(strftime('%s', o.observed_at) - strftime('%s', f.valid_time))
             AS dt_sec
    FROM weather_forecasts f
    JOIN weather_observations o
      ON o.location_id = f.location_id
     AND ABS(strftime('%s', o.observed_at) - strftime('%s', f.valid_time))
         <= :tol
    WHERE o.quality IS NULL OR o.quality != 'suspect'
    """
    params: dict = {"tol": tol_sec}
    if location_id is not None:
        q += " AND f.location_id = :loc"
        params["loc"] = location_id
    if start:
        q += " AND f.valid_time >= :start"
        params["start"] = start
    if end:
        q += " AND f.valid_time <= :end"
        params["end"] = end
    q += " ORDER BY f.id, dt_sec"

    cap = 1 << 20
    X = np.full((cap, n_feat), np.nan, dtype=np.float32)
    y: dict[str, np.ndarray] = {
        t: np.full(cap, np.nan, dtype=np.float32) for t in TASKS
    }
    fh = np.full(cap, np.nan, dtype=np.float32)
    # Per-pair metadata as preallocated arrays (see WeatherDataset):
    # epoch seconds (int64), regime codes (int8), location ids (int64).
    # Timestamps repeat heavily (one per hour), so epoch conversion is
    # cached per distinct string.
    valid_times = np.zeros(cap, dtype=np.int64)
    regimes = np.zeros(cap, dtype=np.int8)
    location_ids = np.zeros(cap, dtype=np.int64)
    vt_epoch_cache: dict[str, int] = {}
    n = 0
    tmin: str | None = None
    tmax: str | None = None
    t_start = time.monotonic()
    last_log = t_start
    print(f"[build_dataset] start db={db_path} location_id={location_id} "
          f"start={start} end={end}", flush=True)

    def _grow() -> None:
        nonlocal cap, X, y, fh, valid_times, regimes, location_ids
        new_cap = cap * 2
        new_X = np.full((new_cap, n_feat), np.nan, dtype=np.float32)
        new_X[:n] = X[:n]
        X = new_X
        for t in TASKS:
            arr = np.full(new_cap, np.nan, dtype=np.float32)
            arr[:n] = y[t][:n]
            y[t] = arr
        new_fh = np.full(new_cap, np.nan, dtype=np.float32)
        new_fh[:n] = fh[:n]
        fh = new_fh
        new_vt = np.zeros(new_cap, dtype=np.int64)
        new_vt[:n] = valid_times[:n]
        valid_times = new_vt
        new_rg = np.zeros(new_cap, dtype=np.int8)
        new_rg[:n] = regimes[:n]
        regimes = new_rg
        new_loc = np.zeros(new_cap, dtype=np.int64)
        new_loc[:n] = location_ids[:n]
        location_ids = new_loc
        cap = new_cap
        print(f"[build_dataset] grow cap={cap} rows={n} "
              f"rss_mb={_rss_mb():.0f}", flush=True)

    conn = sqlite3.connect(db_path)
    # Stream the join and keep only the nearest observation per forecast
    # (ORDER BY f.id, dt_sec makes the first row per fid the nearest).
    # Leakage guard (observed_at <= valid_time + tolerance) is applied
    # inline per pair, exactly as check_pairs() did it.
    # SELECT column positions: 0 fid, 1 location_id, 2 model,
    # 3 forecast_hour, 4 valid_time, 5..11 f_*, 12..16 o_*,
    # 17 observed_at, 18 dt_sec.
    cur_fid = None
    for r in conn.execute(q, params):
        fid = r[0]
        if fid == cur_fid:
            continue  # not the nearest observation for this forecast
        cur_fid = fid
        vt = r[4]
        oa = r[17]
        if not pair_is_valid(vt, oa, tolerance_min):
            continue
        if n == cap:
            _grow()
        f_hour = r[3]
        model = r[2]
        loc_id = r[1]
        f_cloud = r[11]
        f_precip = r[9]
        f_prob = r[10]
        X[n, 0] = _fnum(r[5])
        X[n, 1] = _fnum(r[6])
        X[n, 2] = _fnum(r[7])
        X[n, 3] = _fnum(r[8])
        X[n, 4] = _fnum(f_precip)
        X[n, 5] = _fnum(f_cloud)
        X[n, 6] = _fnum(f_hour)
        X[n, 7] = _hour_of_day(vt)
        j = 8
        matched = False
        for m in MODELS:
            is_m = model == m
            matched = matched or is_m
            X[n, j] = 1.0 if is_m else 0.0
            j += 1
        X[n, j] = 0.0 if matched else 1.0  # model_other
        j += 1
        regime = classify_regime(f_cloud, f_precip, f_prob)
        regimes[n] = REGIME_CODE[regime]
        for rg in REGIMES:
            X[n, j] = 1.0 if regime == rg else 0.0
            j += 1
        epoch = vt_epoch_cache.get(vt)
        if epoch is None:
            epoch = iso_to_epoch(vt)
            vt_epoch_cache[vt] = epoch
        valid_times[n] = epoch
        location_ids[n] = loc_id
        fh[n] = _fnum(f_hour)
        o_temp = r[12]
        o_hum = r[13]
        o_pres = r[14]
        o_wind = r[15]
        o_precip = r[16]
        y["temperature"][n] = _fnum(o_temp)
        y["wind"][n] = _fnum(o_wind)
        y["pressure"][n] = _fnum(o_pres)
        y["humidity"][n] = _fnum(o_hum)
        y["precip_amount"][n] = _fnum(o_precip)
        y["rain_prob"][n] = (
            1.0 if (o_precip is not None and o_precip >= RAIN_THRESHOLD_MM)
            else (0.0 if o_precip is not None else np.nan)
        )
        if tmin is None or vt < tmin:
            tmin = vt
        if tmax is None or vt > tmax:
            tmax = vt
        n += 1
        # Progress log (unbuffered): a future OOM/timeout death leaves a
        # trail of how far the stream got. Checked cheaply every row;
        # the monotonic() call costs ~100 ns, negligible vs row work.
        if n % 10000 == 0:
            now = time.monotonic()
            if now - last_log >= 60.0:
                last_log = now
                print(f"[build_dataset] rows={n} cap={cap} "
                      f"rss_mb={_rss_mb():.0f} "
                      f"elapsed_s={now - t_start:.0f}", flush=True)
    conn.close()

    # Slice to exact size (views, no copy).
    X = X[:n]
    fh = fh[:n]
    valid_times = valid_times[:n]
    regimes = regimes[:n]
    location_ids = location_ids[:n]
    for t in TASKS:
        y[t] = y[t][:n]

    tmin_s = tmin or ""
    tmax_s = tmax or ""
    params = {
        "location_id": location_id, "start": start, "end": end,
        "tolerance_min": tolerance_min,
        "feature_version": "v1",
        "rain_threshold_mm": RAIN_THRESHOLD_MM,
    }
    digest = hashlib.sha256(
        (json.dumps(params, sort_keys=True)
         + f"|{n}|{tmin_s}|{tmax_s}").encode()
    ).hexdigest()[:16]
    dataset_version = f"ds_{digest}"
    print(f"[build_dataset] done rows={n} cap={cap} "
          f"peak_rss_mb={_rss_mb():.0f} "
          f"elapsed_s={time.monotonic() - t_start:.1f} "
          f"version={dataset_version}", flush=True)
    return WeatherDataset(
        X=X, feature_names=names, y=y, regimes=regimes,
        valid_times=valid_times, location_ids=location_ids,
        forecast_hours=fh, dataset_version=dataset_version,
        params=params, n_pairs=n, time_min=tmin_s, time_max=tmax_s,
    )
