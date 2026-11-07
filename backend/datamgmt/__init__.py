"""AETHER data management layer (read-only).

Inventory, quality, maintenance and export over the operational SQLite
database. There is deliberately NO delete/prune/update functionality in
this package: training data (weather_forecasts / weather_observations)
is never deleted, and all other tables are managed by their owning
pipelines. The only mutating operations are SQLite maintenance verbs
(integrity_check / foreign_key_check are read-only; ANALYZE refreshes
planner statistics; VACUUM rewrites the file without dropping data).
"""
from __future__ import annotations

from backend.datamgmt import export, inventory, maintenance, quality  # noqa: F401
