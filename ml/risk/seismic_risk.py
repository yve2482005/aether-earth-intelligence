"""Historical seismicity aggregation for hazard context.

EXPLICIT NON-PREDICTION NOTICE
------------------------------
This module computes descriptive statistics over PAST earthquake events
(counts, maximum magnitude, depth statistics, rates). It is labeled RISK
(hazard context) in the AETHER kind taxonomy.

It does NOT:
  * predict future earthquakes (no future time, location, or magnitude),
  * estimate probabilities of future events,
  * issue warnings, advisories, or alerts.

Any consumer that presents these numbers must label them as historical
hazard context. Aftershock outlooks, where offered, come only from official
or published statistical sources and are labeled "probability forecast,
not a guarantee" — this module does not produce them.
"""

from __future__ import annotations

import sqlite3
import statistics
from datetime import datetime, timezone


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def region_hazard_stats(
    db_path: str,
    min_lat: float,
    max_lat: float,
    min_lon: float,
    max_lon: float,
    since: str | None = None,
    min_magnitude: float = 0.0,
) -> dict:
    """Aggregate historical seismicity inside a bounding box.

    Returns counts, magnitude/depth summaries, and the observed time span.
    kind is always "RISK" (hazard context) — never FORECAST or PREDICTION.
    """
    q = """
    SELECT magnitude, magnitude_type, depth, origin_time, region,
           tsunami_flag
    FROM earthquake_events
    WHERE latitude BETWEEN :la0 AND :la1
      AND longitude BETWEEN :lo0 AND :lo1
      AND magnitude IS NOT NULL AND magnitude >= :mmin
    """
    params: dict = {
        "la0": min_lat, "la1": max_lat, "lo0": min_lon, "lo1": max_lon,
        "mmin": min_magnitude,
    }
    if since:
        q += " AND origin_time >= :since"
        params["since"] = since
    q += " ORDER BY origin_time"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(q, params).fetchall()]
    conn.close()

    mags = [r["magnitude"] for r in rows]
    depths = [r["depth"] for r in rows if r["depth"] is not None]
    times = [r["origin_time"] for r in rows]
    years = 0.0
    if len(times) >= 2:
        t0 = datetime.fromisoformat(times[0].replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(times[-1].replace("Z", "+00:00"))
        years = max((t1 - t0).total_seconds() / 31557600.0, 1e-9)

    stats = {
        "kind": "RISK",
        "kind_detail": "historical seismicity aggregation (hazard context)",
        "is_prediction": False,
        "disclaimer": (
            "Historical hazard context only. This does not predict future "
            "earthquakes: no future time, location, or magnitude is "
            "estimated. Rates are observed past rates, not forecasts."
        ),
        "bbox": {
            "min_lat": min_lat, "max_lat": max_lat,
            "min_lon": min_lon, "max_lon": max_lon,
        },
        "since": since,
        "min_magnitude": min_magnitude,
        "event_count": len(rows),
        "time_span": {
            "first": times[0] if times else None,
            "last": times[-1] if times else None,
            "years": round(years, 2),
        },
        "observed_rate_per_year": round(len(rows) / years, 3) if years else None,
        "magnitude": {
            "max": max(mags) if mags else None,
            "mean": round(statistics.fmean(mags), 2) if mags else None,
            "count_ge_5": sum(1 for m in mags if m >= 5.0),
            "count_ge_6": sum(1 for m in mags if m >= 6.0),
            "count_ge_7": sum(1 for m in mags if m >= 7.0),
        },
        "depth_km": {
            "mean": round(statistics.fmean(depths), 1) if depths else None,
            "median": round(statistics.median(depths), 1) if depths else None,
            "max": max(depths) if depths else None,
            "n_with_depth": len(depths),
        },
        "tsunami_flag_count": sum(1 for r in rows if r["tsunami_flag"]),
        "computed_at": _utcnow(),
    }
    return stats


def render_markdown(stats: dict) -> str:
    m, d = stats["magnitude"], stats["depth_km"]
    return "\n".join([
        "# Seismic hazard context (historical, NOT a prediction)",
        "",
        f"Region bbox: {stats['bbox']}",
        f"Events in catalog window: {stats['event_count']} "
        f"({stats['time_span']['first']} → {stats['time_span']['last']})",
        f"Observed rate: {stats['observed_rate_per_year']} events/year "
        f"(past rate, not a forecast)",
        f"Max magnitude observed: {m['max']} | "
        f"M≥5: {m['count_ge_5']}, M≥6: {m['count_ge_6']}, "
        f"M≥7: {m['count_ge_7']}",
        f"Depth (km): mean {d['mean']}, median {d['median']}, "
        f"max {d['max']} (n={d['n_with_depth']})",
        "",
        stats["disclaimer"],
    ])
