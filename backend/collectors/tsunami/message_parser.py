"""Tsunami message parser.

Parses CAP-ish fields from NWS atom entries / official text. The ORIGINAL
classification is ALWAYS preserved in `raw_classification` and stored verbatim.
Official categories (WARNING / ADVISORY / WATCH / INFORMATION / NO THREAT) are
never translated in meaning — the parser keeps the original string.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, Optional

from backend.common.timeutil import parse_iso, utc_iso

OFFICIAL_CATEGORIES = ("TSUNAMI INFORMATION STATEMENT", "TSUNAMI THREAT",
                       "NO THREAT", "TSUNAMI WARNING", "TSUNAMI ADVISORY",
                       "TSUNAMI WATCH", "TSUNAMI INFORMATION")


def detect_category(title: str, summary: str = "") -> str:
    text = f"{title} {summary}".upper()
    # Cancellation first: a "Tsunami Warning Cancellation" contains the
    # WARNING phrase too, and must not classify as a warning.
    if "CANCEL" in text:
        return "CANCEL"
    # Order matters: specific product phrases before generic ones.
    # "TSUNAMI WARNING" appears in system/center names (UNESCO/IOC Pacific
    # Tsunami Warning and Mitigation System; NWS Pacific Tsunami Warning
    # Center), so it must NOT be checked first — it would misclassify
    # threat messages and no-threat information statements alike.
    # "TSUNAMI THREAT" is checked before "NO THREAT" because PTWC threat
    # messages can say "NO TSUNAMI THREAT TO <place>" for distant coasts
    # while still being a threat message; pure no-threat determinations
    # arrive as information statements, caught by the first entry.
    for cat in OFFICIAL_CATEGORIES:
        if cat in text:
            return cat
    return "UNKNOWN"


_TEST_PHRASES = (
    "FOR TEST PURPOSES ONLY",
    "THIS MESSAGE IS FOR TEST",
    "THIS IS A TEST",
    "TEST MESSAGE",
)


def is_test_message(title: str, summary: str = "") -> bool:
    """True when an official message is a test/exercise transmission.

    NWS test transmissions reuse the real category wording — the stored
    event nws-68f99fa5d1efaa23 is titled 'TEST Tsunami Warning issued ...'
    and its body ends '...THIS_MESSAGE_IS_FOR_TEST_PURPOSES_ONLY...' — so
    category matching alone escalates them to critical alerts. Markers:
    a leading TEST token on the title, or an explicit test-purpose phrase
    anywhere in title/summary (underscores normalized to spaces, since
    NWS products use both spellings). The original wording is still
    preserved verbatim in raw_classification / raw_text by the parsers.
    """
    head = (title or "").strip().upper()
    if head.startswith("TEST") and (len(head) == 4 or not head[4].isalnum()):
        return True
    text = f"{title} {summary}".upper().replace("_", " ")
    return any(p in text for p in _TEST_PHRASES)


def extract_arrival_mention(text: str) -> Optional[str]:
    """Return the raw arrival-mention sentence if present; never invent one."""
    if not text:
        return None
    m = re.search(r"([^.\n]*estimated[^.\n]*arrival[^.\n]*\.?)", text, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m = re.search(r"([^.\n]*arrival[^.\n]*estimated[^.\n]*\.?)", text, re.IGNORECASE)
    return m.group(1).strip() if m else None


def parse_atom_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    title = entry.get("title") or ""
    summary = entry.get("summary") or ""
    category = detect_category(title, summary)
    issue_time = utc_iso(parse_iso(entry.get("updated")))
    tid_src = entry.get("id") or title
    tid = "nws-" + hashlib.sha256(tid_src.encode("utf-8")).hexdigest()[:16]
    cap_status = str(entry.get("cap_status") or "").strip().lower()
    is_test = (cap_status in ("test", "exercise")
               or is_test_message(title, summary))
    if category == "CANCEL":
        status = "cancelled"
    elif is_test:
        status = "test"
    else:
        status = "active"
    return {
        "tsunami_event_id": tid,
        "source": "nws",
        "message_type": category,          # original classification preserved
        "raw_classification": title.strip(),  # verbatim original
        "severity": "TEST" if status == "test" else category,
        "affected_region": None,           # atom feed carries no structured area; see raw_text
        "issue_time": issue_time,
        "valid_time": None,
        "arrival_time": extract_arrival_mention(summary),
        "status": status,
        "source_url": entry.get("link"),
        "raw_text": f"{title}\n{summary}".strip(),
        "kind": "WARNING",
    }


def parse_official_text(text: str, source: str = "official",
                        source_url: Optional[str] = None) -> Dict[str, Any]:
    """Parse a raw official message text; classification preserved verbatim.

    PTWC/NTWC bulletins for one event share the event number in the URL
    (.../events/PHEB/2026/10/24/26297000/2/WEPA40/WEPA40.txt), so message
    #1 (threat) and #2 (passed) update ONE event row instead of two.
    issue_time comes from the bulletin header ("0328 UTC SAT OCT 24 2026"),
    not ingest time. A message stating the threat has passed stores
    status='expired' (documented vocabulary: active|expired|cancelled|
    superseded) so the alert engine never alerts on it.
    """
    category = detect_category(text)
    tid = None
    if source_url:
        m = _PTWC_EVENT_RE.search(source_url)
        if m:
            tid = ("ptwc-" if m.group(1) == "PHEB" else "ntwc-") + m.group(2)
    if tid is None:
        tid = "official-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    is_test = is_test_message(text)
    upper = (text or "").upper()
    if "HAS NOW PASSED" in upper:
        status = "expired"
    elif category == "CANCEL":
        status = "cancelled"
    elif is_test:
        status = "test"
    else:
        status = "active"
    return {
        "tsunami_event_id": tid,
        "source": source,
        "message_type": category,
        "raw_classification": (text.splitlines()[0] if text else "").strip()[:280],
        "severity": "TEST" if is_test else category,
        "affected_region": None,
        "issue_time": _official_issue_time(text) or utc_iso(),
        "valid_time": None,
        "arrival_time": extract_arrival_mention(text),
        "status": status,
        "source_url": source_url,
        "raw_text": text,
        "kind": "WARNING",
    }


# PTWC/NTWC bulletin URL carries the event number:
# .../events/PHEB/2026/10/24/26297000/2/WEPA40/WEPA40.txt
_PTWC_EVENT_RE = re.compile(r"/events/(PHEB|PAAQ)/\d{4}/\d{2}/\d{2}/(\d+)/")
# Bulletin header time: "0328 UTC SAT OCT 24 2026" (also "0237 UTC ...").
_HEADER_TIME_RE = re.compile(r"(\d{4})\s+UTC\s+[A-Z]{3}\s+([A-Z]{3})\s+(\d{1,2})\s+(\d{4})")
_MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
           "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}


def _official_issue_time(text: str) -> Optional[str]:
    """Issue time from the bulletin header, or None (caller falls back
    to ingest time). Never invent a time."""
    m = _HEADER_TIME_RE.search(text or "")
    if not m:
        return None
    month = _MONTHS.get(m.group(2))
    if not month:
        return None
    try:
        hh, mm = int(m.group(1)[:2]), int(m.group(1)[2:])
        day, year = int(m.group(3)), int(m.group(4))
        if not (0 <= hh <= 23 and 0 <= mm <= 59 and 1 <= day <= 31):
            return None
    except ValueError:
        return None
    return f"{year:04d}-{month:02d}-{day:02d}T{hh:02d}:{mm:02d}:00Z"
