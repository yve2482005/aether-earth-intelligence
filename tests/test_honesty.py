"""Scientific-honesty tests: forbidden phrases absent from user-facing strings.

Banned (CONTRACTS.md, enforced here):
  - "100% accurate" / any "100%" accuracy claim
  - "guaranteed" (certainty about earth systems)
  - "earthquake will ..." (prediction of a future quake)
  - "tsunami will occur" (non-authoritative tsunami assertion)

Scans user-visible layers: web/, backend/api/, backend/integrations, docs/,
README.md, HONESTY.md. Following the same convention as the ML team's
tests/test_ml_honesty.py, a phrase is ALLOWED when it appears inside an
explicit ban statement (the surrounding line says "never" or "forbidden") —
documenting the ban is not making the claim.
"""

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

FORBIDDEN = [
    re.compile(r"\b100\s*%\s*(accurate|accuracy|reliable|correct)\b", re.IGNORECASE),
    re.compile(r"\bguaranteed\b", re.IGNORECASE),
    re.compile(r"\bearthquake will\b", re.IGNORECASE),
    re.compile(r"\btsunami will occur\b", re.IGNORECASE),
]

# paths that contain user-facing strings (scan only what exists)
CANDIDATE_DIRS = ["web", "backend/api", "backend/integrations", "docs", "gis"]
CANDIDATE_FILES = ["README.md", "HONESTY.md"]
SCAN_EXTS = {".py", ".md", ".html", ".js", ".json", ".txt"}


def _user_facing_files():
    for name in CANDIDATE_FILES:
        p = os.path.join(ROOT, name)
        if os.path.isfile(p):
            yield p
    for d in CANDIDATE_DIRS:
        base = os.path.join(ROOT, d)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [x for x in dirnames if x not in {"__pycache__"}]
            for fname in filenames:
                _, ext = os.path.splitext(fname)
                if ext.lower() in SCAN_EXTS:
                    yield os.path.join(dirpath, fname)


def _is_ban_statement(text, start, end):
    line = text[max(0, start - 100):end + 60].lower()
    return "never" in line or "forbidden" in line


def test_no_forbidden_certainty_phrases():
    hits = []
    for path in _user_facing_files():
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            text = fh.read()
        for pat in FORBIDDEN:
            for m in pat.finditer(text):
                if _is_ban_statement(text, m.start(), m.end()):
                    continue
                lineno = text.count("\n", 0, m.start()) + 1
                hits.append("%s:%d: %s" % (os.path.relpath(path, ROOT), lineno,
                                           m.group(0)))
    assert not hits, "forbidden certainty phrases found: %s" % hits


def test_honesty_categories_distinct_in_docs():
    # the six honesty labels must appear as DISTINCT terms in the docs
    docs_dir = os.path.join(ROOT, "docs")
    if not os.path.isdir(docs_dir):
        pytest.skip("docs/ not landed yet")
    corpus = ""
    for dirpath, _, filenames in os.walk(docs_dir):
        for fname in filenames:
            if fname.endswith(".md"):
                with open(os.path.join(dirpath, fname), encoding="utf-8", errors="ignore") as fh:
                    corpus += fh.read() + "\n"
    for label in ["FORECAST", "DETECTION", "OBSERVATION", "WARNING", "RISK", "PREDICTION"]:
        assert label in corpus, "docs never mention the distinct label: %s" % label


def test_docs_state_no_earthquake_prediction():
    honesty = os.path.join(ROOT, "HONESTY.md")
    if not os.path.isfile(honesty):
        pytest.skip("HONESTY.md not landed yet")
    with open(honesty, encoding="utf-8") as fh:
        text = fh.read()
    assert re.search(r"earthquake", text, re.IGNORECASE)
    assert re.search(r"prediction", text, re.IGNORECASE)
    assert "aftershock" in text.lower()
