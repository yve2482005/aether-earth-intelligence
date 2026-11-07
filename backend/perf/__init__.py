"""AETHER performance track (workstream F): ultra-fast collection/processing.

Modules:
  parallel      ThreadPoolExecutor fan-out over existing collectors + circuit breakers
  schedule      per-source poll cadence honoring real source terms
  priority      P0-P3 lane queues with starvation guards
  incremental   ETag/Last-Modified conditional fetch + last-timestamp cursors
  ids           content-hash idempotency keys
  cache         TTL cache abstraction (SQLite store; Redis future plug-in)
  precompute    per-location home bundles (GET /v1/home/{id})
  geo           H3 spatial indexing for forecast grid points / map clustering
  ml_batch      batch feature assembly + single predict call per model (no request-path training)
  notify_queue  persistent priority notification lanes
  metrics       per-stage latency + p50/p95/p99 accumulators
"""
from __future__ import annotations

__all__ = ["parallel", "schedule", "priority", "incremental", "ids", "cache",
           "precompute", "geo", "ml_batch", "notify_queue", "metrics"]
