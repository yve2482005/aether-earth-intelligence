"""Generic deduplication helpers."""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Tuple


def dedup_records(records: List[Dict[str, Any]], key_fn: Callable[[Dict[str, Any]], Any]) -> List[Dict[str, Any]]:
    """Keep first occurrence of each key, preserve order."""
    seen = set()
    out: List[Dict[str, Any]] = []
    for r in records:
        k = key_fn(r)
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


def partition_new(existing_keys: set, records: List[Dict[str, Any]],
                  key_fn: Callable[[Dict[str, Any]], Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split records into (new, already_seen)."""
    new, seen = [], []
    for r in records:
        (new if key_fn(r) not in existing_keys else seen).append(r)
    return new, seen


def merge_id_lists(existing_json: str, new_ids: List[str]) -> str:
    """Merge JSON id arrays (order-preserving, de-duplicated)."""
    import json
    try:
        cur = json.loads(existing_json) if existing_json else []
    except (ValueError, TypeError):
        cur = []
    for i in new_ids:
        if i not in cur:
            cur.append(i)
    return json.dumps(cur)
