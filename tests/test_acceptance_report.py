"""Acceptance report (§119): aggregate verdicts -> docs/ACCEPTANCE_REPORT.md.

Runs last (alphabetical). Asserts every category 84.1-118 has a recorded
verdict; missing verdicts become FAIL. Evaluates the §121 release gate and
writes the report with the four required sections using REAL measured
numbers only.

Release gate rule: production-ready is declared ONLY if every verifiable
gate passes. BLOCKED items are documented blockers, not failures.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.test_acceptance_lib import CATEGORIES, load_results  # noqa: E402

PERFORMANCE_CATS = [84.1, 85, 87, 88, 89, 90, 91, 92, 93, 94, 95, 98, 100,
                    106, 107, 108, 109, 110]
DATA_QUALITY_CATS = [86, 99, 101, 102, 103, 104, 105, 114, 115]
FORECAST_QUALITY_CATS = [111, 112]
ALERT_QUALITY_CATS = [96, 97, 113, 118]
BLOCKED_EXPECTED = {92.5, 116, 117}

ZERO_DEFECT = [
    ("0 duplicate critical alerts", 99),
    ("0 fabricated warnings (no invented arrival times, no skipped validation)", (97, 105)),
    ("0 exposed secrets", 114),
    ("provenance preserved", 115),
]


def _fmt_evidence(ev):
    if not ev:
        return "—"
    return "; ".join(f"{k}={v}" for k, v in ev.items())


def test_generate_acceptance_report():
    results = load_results()
    # missing verdicts -> FAIL (§120: never fabricate a PASS)
    for cat in CATEGORIES:
        if cat not in results:
            results[cat] = {"category": cat, "name": CATEGORIES[cat],
                            "verdict": "FAIL",
                            "fail": {"metric": "verdict recording",
                                     "actual": "no verdict recorded",
                                     "target": "recorded PASS/BLOCKED",
                                     "difference": "n/a",
                                     "probable_cause": "test did not run or crashed",
                                     "recommended_fix": "run the acceptance suite fully"}}

    def section(title, cats):
        lines = [f"### {title}", ""]
        for c in cats:
            r = results[c]
            lines.append(f"- **{c} {r['name']}** — **{r['verdict']}**")
            lines.append(f"  - evidence: {_fmt_evidence(r.get('evidence'))}")
            if r["verdict"] == "FAIL":
                f = r.get("fail", {})
                lines.append(
                    f"  - FAIL detail: metric={f.get('metric')} actual={f.get('actual')} "
                    f"target={f.get('target')} difference={f.get('difference')} "
                    f"cause={f.get('probable_cause')} fix={f.get('recommended_fix')}")
            if r["verdict"] == "BLOCKED":
                b = r.get("blocked", {})
                lines.append(
                    f"  - BLOCKED: dependency={b.get('external_dependency')} "
                    f"why={b.get('why_it_blocks')} workaround={b.get('workaround')}")
        return "\n".join(lines)

    counts = {"PASS": 0, "FAIL": 0, "BLOCKED": 0}
    for r in results.values():
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1

    # §121 release gate
    gate_rows = []
    gate_ok = True
    for label, cats in ZERO_DEFECT:
        cats = (cats,) if isinstance(cats, (int, float)) else cats
        verdicts = [results[c]["verdict"] for c in cats]
        ok = all(v == "PASS" for v in verdicts)
        gate_ok = gate_ok and ok
        gate_rows.append(f"- {label}: {'PASS' if ok else 'FAIL'}")
    fails = [c for c, r in results.items() if r["verdict"] == "FAIL"]
    gate_ok = gate_ok and not fails
    release = ("PASS — every verifiable gate passes; BLOCKED items are documented "
               "environment/ownership limits, not failures" if gate_ok
               else "GATED — not production-ready: "
                    + ", ".join(f"{c} {results[c]['name']}" for c in fails))

    doc = f"""# AETHER Performance Track — Acceptance Report

Perf spec §§84–122. Generated from automated acceptance tests
(`tests/test_acceptance_*.py`); every number below is a real measurement from
that run. PASS/FAIL/BLOCKED per §120. Verdict counts:
PASS={counts['PASS']} FAIL={counts['FAIL']} BLOCKED={counts['BLOCKED']}.

Honesty notes: synthetic fixtures are labeled SYNTHETIC and run in temporary
databases — never production tables or real alert channels. Redis/PostGIS are
documented future plug-ins, never claimed running. External latency is outside
our control (§60); all latencies are measured, never fabricated.

{section('Performance', PERFORMANCE_CATS)}

{section('Data Quality', DATA_QUALITY_CATS)}

{section('Forecast Quality', FORECAST_QUALITY_CATS)}

{section('Alert Quality', ALERT_QUALITY_CATS)}

### Environment-blocked items (not failures)

"""
    for c in sorted(BLOCKED_EXPECTED):
        r = results.get(c)
        if r is None or r["verdict"] != "BLOCKED":
            # writer never ran (e.g. subset run): honest FAIL, never KeyError
            results[c] = {"category": c, "name": CATEGORIES.get(c, "?"),
                          "verdict": "FAIL",
                          "fail": {"metric": "verdict recording",
                                   "actual": "no BLOCKED verdict recorded",
                                   "target": "recorded BLOCKED with reason",
                                   "difference": "n/a",
                                   "probable_cause": "category test did not run",
                                   "recommended_fix": "run the full acceptance suite"}}
            r = results[c]
        b = r.get("blocked", {})
        doc += (f"- **{c} {r['name']}** — {r['verdict']}: {b.get('external_dependency')}. "
                f"{b.get('why_it_blocks')} Workaround: {b.get('workaround')}\n")

    doc += f"""
### Release gate (§121)

Zero-defect checks:
{chr(10).join(gate_rows)}

**Release gate: {release}**

### What was NOT tested (honest limits)

- Real FCM delivery (no device tokens; held honestly, never faked)
- PostGIS query plans (deployment is SQLite)
- Multi-host / distributed behavior (single-host SQLite model)
- Adversarial / high-throughput load (moderate concurrency only: 20–100 threads)
- UI performance + accessibility (owned by UI workstream)
- Live source endpoints from sandbox (no network: USGS ETag check skipped)
- 1M-record map benchmarks (VM-sized: 5000-event cap test used)
"""
    out = ROOT / "docs" / "ACCEPTANCE_REPORT.md"
    out.write_text(doc, encoding="utf-8")
    assert out.exists()
    assert not fails, f"acceptance FAILs: {fails}"
    print(f"\nACCEPTANCE: PASS={counts['PASS']} FAIL={counts['FAIL']} "
          f"BLOCKED={counts['BLOCKED']} -> {out}")
