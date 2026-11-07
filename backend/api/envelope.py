"""Standard v2 response envelope (§78).

NEW v2 endpoints wrap their payload with `envelope(...)` so clients always
see where a datum came from, how fresh it is, and how much to trust it.
Existing v1 endpoints are NOT rewrapped — their response shapes are pinned
by tests/test_api_contract.py.

Envelope fields:
    data            the payload (dict or list)
    source          source identifier(s), e.g. "open-meteo" or ["usgs"]
    source_timestamp when the source produced the data (UTC ISO-8601), if known
    retrieved_at    when the API read the data (UTC ISO-8601)
    processed_at    when the API finished processing (UTC ISO-8601)
    model_version   ML/model version string, or None for non-model data
    confidence      high|medium|low|unknown — never fabricated; pass through
                    what backend/confidence/engine.py computed
    data_quality    ok|degraded|unknown — per the validate-data / data_quality log
    status          ok|unavailable|partial|error
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

from backend.common.timeutil import utc_iso

SourceSpec = Union[str, List[str], None]


def envelope(data: Any,
             source: SourceSpec = None,
             source_timestamp: Optional[str] = None,
             model_version: Optional[str] = None,
             confidence: Optional[str] = None,
             data_quality: Optional[str] = None,
             status: str = "ok") -> Dict[str, Any]:
    """Build the standard v2 envelope around `data`.

    `retrieved_at`/`processed_at` default to "now" (envelope construction
    time). Routes that know the real retrieval/processing times should pass
    explicit values for full honesty.
    """
    now = utc_iso()
    sources = source if isinstance(source, list) else ([source] if source else [])
    return {
        "data": data,
        "source": sources,
        "source_timestamp": source_timestamp,
        "retrieved_at": now,
        "processed_at": now,
        "model_version": model_version,
        "confidence": confidence or "unknown",
        "data_quality": data_quality or "unknown",
        "status": status,
    }


def unavailable_envelope(message: str, source: SourceSpec = None,
                         source_timestamp: Optional[str] = None) -> Dict[str, Any]:
    """Honest envelope for missing/unavailable data — never fake a payload."""
    env = envelope({"message": message}, source=source,
                   source_timestamp=source_timestamp, status="unavailable")
    return env
