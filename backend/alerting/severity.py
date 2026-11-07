"""Severity mapping for official and observed hazard signals.

Canonical severities: info | advisory | watch | warning | critical
"""
from __future__ import annotations

from typing import Optional

# NWS CAP / official severity strings -> canonical.
OFFICIAL_MAP = {
    "EXTREME": "critical",
    "SEVERE": "warning",
    "MODERATE": "advisory",
    "MINOR": "info",
    "UNKNOWN": "info",
    "TSUNAMI WARNING": "critical",
    "TSUNAMI ADVISORY": "warning",
    "TSUNAMI WATCH": "watch",
    "TSUNAMI INFORMATION": "info",
    "TSUNAMI INFORMATION STATEMENT": "info",
    "NO THREAT": "info",
    "CANCEL": "info",
}

# USGS PAGER alert colors -> canonical.
PAGER_MAP = {"red": "critical", "orange": "warning", "yellow": "advisory", "green": "info"}


# ---- v2 six-level scale (spec §11, ADDITIVE — existing functions untouched) ----
NEW_SEVERITIES = ("INFORMATION", "LOW", "MODERATE", "HIGH", "SEVERE", "LIFE_SAFETY")

# Forward map: legacy canonical -> v2 scale.
FORWARD_MAP = {
    "info": "INFORMATION",
    "advisory": "LOW",
    "watch": "MODERATE",
    "warning": "HIGH",
    "critical": "SEVERE",
}

_NEW_RANK = {s: i for i, s in enumerate(NEW_SEVERITIES)}
_OLD_RANK = {"info": 0, "advisory": 1, "watch": 2, "warning": 3, "critical": 4}


def to_new_severity(old: Optional[str], life_safety: bool = False) -> str:
    """Map a legacy severity (or v2 name) forward to the v2 six-level scale.

    life_safety=True is reserved for official life-safety warnings and
    verified life-threatening observations; it is the only path to LIFE_SAFETY.
    """
    if old is None:
        return "INFORMATION"
    s = str(old).strip().upper()
    if s in NEW_SEVERITIES:
        return s
    if life_safety and s == "CRITICAL":
        return "LIFE_SAFETY"
    return FORWARD_MAP.get(s.lower(), "INFORMATION")


def severity_rank(sev: Optional[str]) -> int:
    """Numeric rank on the v2 scale (0=INFORMATION .. 5=LIFE_SAFETY).

    Accepts legacy lowercase names too (mapped forward)."""
    return _NEW_RANK[to_new_severity(sev)]


def legacy_rank(sev: Optional[str]) -> int:
    """Numeric rank on the legacy scale (0=info .. 4=critical)."""
    return _OLD_RANK.get(str(sev or "").strip().lower(), 0)


def is_meaningful_escalation(old: Optional[str], new: Optional[str]) -> bool:
    """True only when severity moved to a strictly higher level.

    "M6.2 -> M6.4 -> M6.5" with the same mapped severity = one event, no
    re-notification. A step up (e.g. LOW -> HIGH) = escalate notification."""
    return severity_rank(new) > severity_rank(old)


def is_life_safety(sev: Optional[str]) -> bool:
    """LIFE_SAFETY level or legacy critical — these break through silent hours
    and feed filtering."""
    return to_new_severity(sev) in ("LIFE_SAFETY", "SEVERE")


def official_to_severity(s: Optional[str]) -> str:
    return OFFICIAL_MAP.get((s or "").strip().upper(), "info")


def pager_to_severity(alert: Optional[str]) -> str:
    return PAGER_MAP.get((alert or "").strip().lower(), "info")


def magnitude_to_severity(mag: Optional[float]) -> str:
    if mag is None:
        return "info"
    if mag >= 7.5:
        return "critical"
    if mag >= 6.5:
        return "warning"
    if mag >= 5.5:
        return "advisory"
    return "info"
