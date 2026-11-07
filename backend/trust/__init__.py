"""AETHER trust package: lineage, data quality, trust index, verification."""
from backend.trust.lineage import build_lineage_chain
from backend.trust.quality import dataset_quality
from backend.trust.trust_index import compute_trust_index
from backend.trust.verification import (
    VerificationError,
    baseline_scores,
    compute_metrics,
    forecast_vs_observed,
    model_comparison,
)

__all__ = [
    "build_lineage_chain",
    "dataset_quality",
    "compute_trust_index",
    "compute_metrics",
    "baseline_scores",
    "forecast_vs_observed",
    "model_comparison",
    "VerificationError",
]
