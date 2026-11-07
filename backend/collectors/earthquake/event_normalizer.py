"""Event normalizer: USGS GeoJSON feature -> canonical earthquake_events row dict."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List


def _ms_to_iso(ms: Any) -> str | None:
    if ms is None:
        return None
    try:
        dt = datetime.fromtimestamp(float(ms) / 1000.0, tz=timezone.utc)
        return dt.isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, OSError):
        return None


def usgs_ids(feature: Dict[str, Any], props: Dict[str, Any]) -> List[str]:
    """Authoritative event id + alias list from the feed."""
    fid = feature.get("id")
    ids: List[str] = []
    if fid:
        ids.append(fid)
    raw = props.get("ids") or ""
    for part in str(raw).split(","):
        part = part.strip()
        if part and part not in ids:
            ids.append(part)
    return ids


def normalize_usgs_feature(feature: Dict[str, Any]) -> Dict[str, Any]:
    """Map a USGS GeoJSON feature to earthquake_events columns."""
    props = feature.get("properties") or {}
    geom = feature.get("geometry") or {}
    coords = geom.get("coordinates") or [None, None, None]
    ids = usgs_ids(feature, props)
    return {
        "canonical_event_id": ids[0] if ids else None,
        "source": "usgs",
        "source_event_ids": json.dumps(ids),
        "origin_time": _ms_to_iso(props.get("time")),
        "latitude": coords[1],
        "longitude": coords[0],
        "depth": coords[2],
        "magnitude": props.get("mag"),
        "magnitude_type": props.get("magType"),
        "region": props.get("place"),
        "status": props.get("status"),
        "review_status": (props.get("type") or "earthquake"),
        "felt_count": props.get("felt"),
        "mmi": props.get("cdi"),
        "alert_level": props.get("alert"),
        "tsunami_flag": int(bool(props.get("tsunami"))),
        "shakemap_available": 0,   # full products need ComCat detail fetch (see shakemap.py)
        "pager_available": 0,      # same; summary feed only carries the 'alert' color
        "source_url": props.get("url"),
        "detail_url": props.get("detail"),
        "update_time": _ms_to_iso(props.get("updated")),
    }
