"""Honesty tests for ML-owned code.

Per CONTRACTS.md scientific-honesty rules: no "100% accurate", "guaranteed",
"zero defects", or certainty-about-the-future language in ML-owned files,
and the seismic module must carry its explicit non-prediction notice.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OWNED = [ROOT / "ml", ROOT / "backend" / "ml_support"]

FORBIDDEN = [
    r"100%\s*accurate",
    r"\bguaranteed\b",
    r"\bzero defects\b",
    r"\bno defects\b",
    r"will definitely",
    r"certain future",
]


def _owned_files():
    for base in OWNED:
        for p in base.rglob("*"):
            if p.is_file() and p.suffix in (".py", ".md"):
                # the synthetic fixture is labeled test data, not a claim
                if "synthetic_smoke" in p.name:
                    continue
                yield p


def test_no_forbidden_certainty_phrases():
    hits = []
    for p in _owned_files():
        text = p.read_text(errors="replace")
        for pat in FORBIDDEN:
            for m in re.finditer(pat, text, re.IGNORECASE):
                # allow the phrase when it appears inside a *ban* statement
                line = text[max(0, m.start() - 80):m.end() + 40]
                if "never" in line.lower() or "no \"" in line.lower():
                    continue
                hits.append(f"{p.relative_to(ROOT)}: {pat!r} -> {line.strip()}")
    assert not hits, "forbidden certainty phrases found:\n" + "\n".join(hits)


def test_seismic_module_carries_non_prediction_notice():
    src = (ROOT / "ml" / "risk" / "seismic_risk.py").read_text()
    assert "NOT" in src and "prediction" in src.lower()
    assert '"is_prediction": False' in src or "'is_prediction': False" in src


def test_seismic_stats_do_not_predict():
    import sqlite3

    from ml.risk.seismic_risk import region_hazard_stats

    db = str(ROOT / "tests" / "_tmp_seis.db")
    Path(db).unlink(missing_ok=True)
    schema = (ROOT / "backend" / "db" / "schema.sql").read_text()
    conn = sqlite3.connect(db)
    conn.executescript(schema)
    conn.execute(
        """INSERT INTO earthquake_events
           (canonical_event_id, source, source_event_ids, origin_time,
            latitude, longitude, depth, magnitude, magnitude_type,
            created_at)
           VALUES ('e1','usgs','["u1"]','2026-01-05T00:00:00Z',
                   16.8,96.1,10.0,5.5,'mw','2026-01-05T00:01:00Z')""")
    conn.execute(
        """INSERT INTO earthquake_events
           (canonical_event_id, source, source_event_ids, origin_time,
            latitude, longitude, depth, magnitude, magnitude_type,
            created_at)
           VALUES ('e2','usgs','["u2"]','2026-06-05T00:00:00Z',
                   17.0,96.3,33.0,6.1,'mw','2026-06-05T00:01:00Z')""")
    conn.commit()
    conn.close()
    try:
        s = region_hazard_stats(db, 16.0, 18.0, 95.5, 97.0)
        assert s["kind"] == "RISK"
        assert s["is_prediction"] is False
        assert s["event_count"] == 2
        assert s["magnitude"]["max"] == 6.1
        assert s["magnitude"]["count_ge_5"] == 2
        assert "not predict" in s["disclaimer"]
    finally:
        Path(db).unlink(missing_ok=True)
