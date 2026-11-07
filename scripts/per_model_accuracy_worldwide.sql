-- Per-MODEL worldwide accuracy, FAST version (exact-hour match).
-- The original per_model_accuracy.sql worldwide sections timed out at 25 min:
-- the ROW_NUMBER window over ~11M joined forecast rows is too slow under
-- load. This version joins observations on the EXACT valid_time hour
-- (nowcast observations are hourly, on the hour, same as forecasts),
-- which removes the window sort entirely. All forecast runs are kept
-- (same all-leads method as the Yangon sections). Brier normalises
-- precip_probability to 0-1 either way (DB scale unverified: 0-1 or 0-100).
-- Tier A only (nowcast reference), valid_time Oct 20 -> now.
-- Never compare ISO 'T...Z' strings against datetime() space format.
.timeout 120000
.mode list
.headers on

SELECT '--- WORLDWIDE Tier A temperature by model (all leads, exact-hour match) ---';
SELECT f.model, COUNT(*) AS n,
  ROUND(AVG(ABS(f.temperature - o.temperature)),4) AS mae,
  ROUND(AVG(f.temperature - o.temperature),4) AS bias,
  ROUND(SQRT(AVG((f.temperature - o.temperature)*(f.temperature - o.temperature))),4) AS rmse
FROM weather_forecasts f
JOIN weather_observations o ON o.location_id = f.location_id AND o.observed_at = f.valid_time
JOIN locations l ON l.id = f.location_id
WHERE l.collect_every_hours = 1
  AND f.valid_time >= '2026-10-20T00:00:00Z'
  AND f.valid_time <= strftime('%Y-%m-%dT%H:%M:%SZ','now')
  AND f.temperature IS NOT NULL AND o.temperature IS NOT NULL
GROUP BY f.model ORDER BY mae;

SELECT '--- WORLDWIDE Tier A rain by model (precip MAE + Brier, exact-hour match) ---';
SELECT f.model, COUNT(*) AS n,
  ROUND(AVG(ABS(f.precipitation - o.precipitation)),4) AS precip_mae,
  ROUND(AVG((CASE WHEN f.precip_probability > 1 THEN f.precip_probability/100.0 ELSE COALESCE(f.precip_probability,0.0) END - (CASE WHEN o.precipitation >= 0.5 THEN 1.0 ELSE 0.0 END)) * (CASE WHEN f.precip_probability > 1 THEN f.precip_probability/100.0 ELSE COALESCE(f.precip_probability,0.0) END - (CASE WHEN o.precipitation >= 0.5 THEN 1.0 ELSE 0.0 END))),5) AS brier
FROM weather_forecasts f
JOIN weather_observations o ON o.location_id = f.location_id AND o.observed_at = f.valid_time
JOIN locations l ON l.id = f.location_id
WHERE l.collect_every_hours = 1
  AND f.valid_time >= '2026-10-20T00:00:00Z'
  AND f.valid_time <= strftime('%Y-%m-%dT%H:%M:%SZ','now')
  AND f.precipitation IS NOT NULL AND o.precipitation IS NOT NULL
GROUP BY f.model ORDER BY brier;
