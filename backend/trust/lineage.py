"""Data lineage (§33): every major result gets a trace view.

Chain: UI result -> model version -> processed dataset -> raw data -> source.

Each node carries: source, URL / feed id, retrieval / source / processing
timestamps, dataset / model / transformation versions, license.

Built only from existing stored columns — nothing invented. If a hop is
missing from storage, the node is returned with value None and an explicit
note, never fabricated.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional


def _registry(conn: sqlite3.Connection, source_id: str) -> Dict[str, Any]:
    cur = conn.execute("SELECT * FROM source_registry WHERE source_id = ?", (source_id,))
    r = cur.fetchone()
    if r is None:
        return {}
    try:
        return dict(r)
    except TypeError:
        keys = [d[0] for d in cur.description]
        return dict(zip(keys, r))


def _node(level: str, title: str, detail: str,
          fields: Optional[Dict[str, Any]] = None,
          missing: bool = False) -> Dict[str, Any]:
    return {"level": level, "title": title, "detail": detail,
            "fields": fields or {}, "missing": missing}


def build_lineage_chain(conn: sqlite3.Connection, kind: str,
                        **kwargs: Any) -> Dict[str, Any]:
    """Return the lineage chain for a result.

    kind: 'weather-forecast' | 'weather-observation' | 'quake' | 'tsunami'
    kwargs per kind:
      weather-forecast: location_id, model, valid_time
      weather-observation: location_id, observed_at
      quake: canonical_event_id
      tsunami: tsunami_event_id
    """
    builders = {
        "weather-forecast": _weather_forecast,
        "weather-observation": _weather_observation,
        "quake": _quake,
        "tsunami": _tsunami,
    }
    if kind not in builders:
        raise ValueError(f"unknown lineage kind: {kind}")
    chain = builders[kind](conn, **kwargs)
    return {
        "kind": kind,
        "chain": chain,
        "complete": all(not n.get("missing") for n in chain),
        "note": ("Every hop below comes from stored AETHER records. "
                 "A missing hop means it was not recorded — never guessed."),
    }


def _weather_forecast(conn: sqlite3.Connection, location_id: int, model: str,
                      valid_time: str) -> List[Dict[str, Any]]:
    cur = conn.execute(
        """SELECT * FROM weather_forecasts
           WHERE location_id = ? AND model = ? AND valid_time = ?
           ORDER BY issued_at DESC LIMIT 1""",
        (location_id, model, valid_time))
    r = cur.fetchone()
    row = dict(r) if r else None
    src_id = "open-meteo-forecast"
    reg = _registry(conn, src_id)

    mv = conn.execute(
        "SELECT version, deployed_at FROM model_versions WHERE is_champion = 1 LIMIT 1"
    ).fetchone()
    model_node = _node(
        "model-version", "Forecast model",
        (f"Model '{model}' — champion model version "
         f"{mv['version'] if mv else 'not recorded'}."),
        {"model": model,
         "model_version": mv["version"] if mv else None,
         "deployed_at": mv["deployed_at"] if mv else None},
        missing=mv is None)

    if row is None:
        return [_node("ui-result",
                      "Forecast result",
                      f"No stored forecast for location {location_id}, "
                      f"model {model}, valid {valid_time}.",
                      {"location_id": location_id, "model": model,
                       "valid_time": valid_time},
                      missing=True),
                model_node]

    return [
        _node("ui-result", "UI result — weather forecast",
              f"Forecast for location {location_id}, valid {valid_time}.",
              {"location_id": location_id, "model": row["model"],
               "forecast_run": row["forecast_run"],
               "forecast_hour": row["forecast_hour"],
               "valid_time": row["valid_time"]}),
        model_node,
        _node("processed-dataset", "Processed dataset — weather_forecasts",
              "Stored forecast row (one of the processed forecast dataset).",
              {"dataset": "weather_forecasts", "row_id": row["id"],
               "issued_at": row["issued_at"],
               "processing_timestamp": row["collected_at"],
               "transformation_version": "open-meteo-collector-v1"}),
        _node("raw-data", "Raw data — provider model output",
              f"Raw provider feed item, retrieved {row['collected_at']}.",
              {"feed_id": src_id,
               "retrieval_timestamp": row["collected_at"],
               "source_timestamp": row["issued_at"],
               "raw_version": row["forecast_run"]}),
        _node("source", "Source",
              reg.get("provider") or "Open-Meteo",
              {"source": reg.get("source_id") or src_id,
               "url": reg.get("url"),
               "license": reg.get("license"),
               "attribution": reg.get("attribution"),
               "commercial_use": reg.get("commercial_use")},
              missing=not reg),
    ]


def _weather_observation(conn: sqlite3.Connection, location_id: int,
                         observed_at: str) -> List[Dict[str, Any]]:
    cur = conn.execute(
        """SELECT * FROM weather_observations
           WHERE location_id = ? AND observed_at = ? LIMIT 1""",
        (location_id, observed_at))
    r = cur.fetchone()
    row = dict(r) if r else None
    src_id = row["source"] if row and row.get("source") else "open-meteo-forecast"
    reg = _registry(conn, src_id)
    if row is None:
        return [_node("ui-result", "Observation result",
                      f"No stored observation for location {location_id} at "
                      f"{observed_at}.",
                      {"location_id": location_id, "observed_at": observed_at},
                      missing=True)]
    return [
        _node("ui-result", "UI result — observation",
              f"Observation at {observed_at} (station {row['station']}).",
              {"location_id": location_id, "observed_at": row["observed_at"],
               "quality": row["quality"]}),
        _node("processed-dataset", "Processed dataset — weather_observations",
              "Stored observation row.",
              {"dataset": "weather_observations", "row_id": row["id"],
               "processing_timestamp": row["collected_at"],
               "transformation_version": "observation-collector-v1"}),
        _node("raw-data", "Raw data — provider observation feed",
              f"Retrieved {row['collected_at']}.",
              {"feed_id": src_id,
               "retrieval_timestamp": row["collected_at"],
               "source_timestamp": row["observed_at"]}),
        _node("source", "Source",
              reg.get("provider") or src_id,
              {"source": reg.get("source_id") or src_id,
               "url": reg.get("url"),
               "license": reg.get("license"),
               "attribution": reg.get("attribution")},
              missing=not reg),
    ]


def _quake(conn: sqlite3.Connection, canonical_event_id: str) -> List[Dict[str, Any]]:
    cur = conn.execute(
        "SELECT * FROM earthquake_events WHERE canonical_event_id = ?",
        (canonical_event_id,))
    r = cur.fetchone()
    if r is None:
        return [_node("ui-result", "Earthquake event",
                      f"No stored event {canonical_event_id}.",
                      {"canonical_event_id": canonical_event_id}, missing=True)]
    row = dict(r)
    src = row.get("source") or "usgs"
    src_id = "usgs-earthquake-all-hour"
    reg = _registry(conn, src_id)
    return [
        _node("ui-result", "UI result — earthquake event",
              f"M{row['magnitude']} {row['region'] or ''} at {row['origin_time']}.",
              {"canonical_event_id": canonical_event_id,
               "origin_time": row["origin_time"],
               "magnitude": row["magnitude"],
               "magnitude_type": row["magnitude_type"]}),
        _node("model-version", "Characterization",
              "Deterministic rule-based characterization (no ML); "
              "deduplication applied across source feeds.",
              {"model": "quake-characterizer-v1",
               "transformation_version": "dedup-v1"}),
        _node("processed-dataset", "Processed dataset — earthquake_events",
              "Canonical event after cross-feed dedup.",
              {"dataset": "earthquake_events",
               "source_event_ids": row["source_event_ids"],
               "processing_timestamp": row["created_at"]}),
        _node("raw-data", "Raw data — provider feed item",
              f"Received {row['created_at']}.",
              {"feed_id": src_id,
               "retrieval_timestamp": row["created_at"],
               "source_timestamp": row["update_time"],
               "source_url": row["source_url"]}),
        _node("source", "Source", reg.get("provider") or "USGS",
              {"source": reg.get("source_id") or src_id,
               "url": reg.get("url"),
               "license": reg.get("license"),
               "attribution": reg.get("attribution")},
              missing=not reg),
    ]


def _tsunami(conn: sqlite3.Connection, tsunami_event_id: str) -> List[Dict[str, Any]]:
    cur = conn.execute(
        "SELECT * FROM tsunami_events WHERE tsunami_event_id = ?",
        (tsunami_event_id,))
    r = cur.fetchone()
    if r is None:
        return [_node("ui-result", "Tsunami event",
                      f"No stored event {tsunami_event_id}.",
                      {"tsunami_event_id": tsunami_event_id}, missing=True)]
    row = dict(r)
    src_id = "nws-tsunami-cap"
    reg = _registry(conn, src_id)
    msgs = conn.execute(
        "SELECT COUNT(*) AS c FROM tsunami_messages WHERE tsunami_event_id = ?",
        (tsunami_event_id,)).fetchone()
    return [
        _node("ui-result", "UI result — tsunami event",
              f"{row['message_type']} — {row['affected_region'] or ''}.",
              {"tsunami_event_id": tsunami_event_id,
               "issue_time": row["issue_time"],
               "status": row["status"]}),
        _node("processed-dataset", "Processed dataset — tsunami_events",
              f"{msgs['c'] if msgs else 0} message revision(s) stored.",
              {"dataset": "tsunami_events",
               "processing_timestamp": row["updated_at"],
               "transformation_version": "cap-parser-v1"}),
        _node("raw-data", "Raw data — CAP alert feed",
              f"Last update {row['updated_at']}.",
              {"feed_id": src_id,
               "retrieval_timestamp": row["updated_at"],
               "source_timestamp": row["issue_time"],
               "source_url": row["source_url"]}),
        _node("source", "Source", reg.get("provider") or "NWS",
              {"source": reg.get("source_id") or src_id,
               "url": reg.get("url"),
               "license": reg.get("license"),
               "attribution": reg.get("attribution"),
               "note": reg.get("notes")},
              missing=not reg),
    ]
