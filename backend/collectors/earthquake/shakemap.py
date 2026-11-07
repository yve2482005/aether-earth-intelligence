"""ShakeMap product flags.

The USGS summary GeoJSON feed does NOT include ShakeMap grids — only the
detail URL per event. This module extracts what the feed does carry
(alert color / PAGER proxy, felt counts) and documents that full ShakeMap /
PAGER / DYFI products require a ComCat detail fetch of properties['detail'].
"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional

from backend.common.http import fetch_json, make_session
from backend.common.timeutil import utc_iso

DETAIL_DOC = ("Full ShakeMap/PAGER/DYFI products are available only via the ComCat "
              "detail URL (properties['detail']) for each event; the summary feed "
              "carries alert color and felt counts only.")


def flags_from_properties(props: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "shakemap_available": False,  # not present in summary feed
        "pager_available": bool(props.get("alert")),
        "pager_alert_color": props.get("alert"),
        "felt_count": props.get("felt"),
        "mmi": props.get("cdi"),
        "detail_url": props.get("detail"),
        "note": DETAIL_DOC,
    }


def mark_product_flags(conn: sqlite3.Connection, canonical_event_id: str,
                       flags: Dict[str, Any]) -> None:
    conn.execute(
        """UPDATE earthquake_events SET shakemap_available = ?, pager_available = ?,
           felt_count = COALESCE(?, felt_count), mmi = COALESCE(?, mmi)
           WHERE canonical_event_id = ?""",
        (1 if flags.get("shakemap_available") else 0,
         1 if flags.get("pager_available") else 0,
         flags.get("felt_count"), flags.get("mmi"), canonical_event_id))
    conn.commit()


def list_detail_products(detail_url: str, session=None) -> Dict[str, Any]:
    """Fetch one ComCat detail document and list available product types.

    Used sparingly (rate-limited by caller) to discover ShakeMap/PAGER/DYFI
    and operational aftershock-forecast products for a significant event.
    """
    session = session or make_session()
    res = fetch_json(session, detail_url)
    if not res["ok"]:
        return {"ok": False, "error": res["error"]}
    products = ((res["data"] or {}).get("properties") or {}).get("products") or {}
    return {"ok": True, "product_types": sorted(products.keys()),
            "has_aftershock_forecast": "aftershock-forecast" in products,
            "has_shakemap": "shakemap" in products,
            "has_pager": any(k.startswith("losspager") for k in products),
            "has_dyfi": "dyfi" in products,
            "checked_at": utc_iso()}
