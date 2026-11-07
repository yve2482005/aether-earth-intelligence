"""Pagination helper for v2 list endpoints (§77).

Cap is 500 items/page (sane default for a SQLite-backed API on a free host).
"""
from __future__ import annotations

import math
from typing import Any, Dict, Optional, Sequence

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 500


def normalize_page(page: Any) -> int:
    try:
        p = int(page)
    except (TypeError, ValueError):
        return 1
    return max(p, 1)


def normalize_page_size(page_size: Any) -> int:
    try:
        s = int(page_size)
    except (TypeError, ValueError):
        return DEFAULT_PAGE_SIZE
    return min(max(s, 1), MAX_PAGE_SIZE)


def paginate(items: Sequence[Any], page: Any = 1, page_size: Any = DEFAULT_PAGE_SIZE,
             total: Optional[int] = None) -> Dict[str, Any]:
    """Paginate a sequence (or a pre-sliced page when `total` is given).

    Returns {items, page, page_size, total, pages, has_next, has_prev}.
    page_size is hard-capped at MAX_PAGE_SIZE.
    """
    p = normalize_page(page)
    s = normalize_page_size(page_size)
    already_sliced = total is not None
    total = len(items) if total is None else max(int(total), 0)
    pages = math.ceil(total / s) if total else 0
    page_items = list(items) if already_sliced else list(items[(p - 1) * s: p * s])
    return {
        "items": page_items,
        "page": p,
        "page_size": s,
        "total": total,
        "pages": pages,
        "has_next": p < pages,
        "has_prev": p > 1,
    }
