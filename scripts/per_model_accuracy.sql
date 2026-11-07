-- Per-MODEL accuracy: raw forecast models vs verifying observations.
-- Same method as per_location_accuracy.sql: nearest observation within
-- 90 min, valid_time Oct 20 -> now, Tier A only (nowcast reference).
-- Never compare ISO 'T...Z' strings against datetime() space format.
.timeout 120000
.mode list
.headers on

SELECT '--- YANGON temperature by model (all leads) ---';
WITH scored AS (
  SELECT f.model, f.temperature AS fc, o.temperature AS ob,
    ROW_NUMBER() OVER (PARTITION BY f.id
      ORDER BY ABS(strftime('%s', o.observed_at) - strftime('%s', f.valid_time))) AS rn
  FROM weather_forecasts f
  JOIN weather_observations o ON o.location_id = f.location_id
    AND o.observed_at BETWEEN strftime('%Y-%m-%dT%H:%M:%SZ', f.valid_time, '-90 minutes')
                          AND strftime('%Y-%m-%dT%H:%M:%SZ', f.valid_time, '+90 minutes')
  WHERE f.location_id = 1
    AND f.valid_time >= '2026-10-20T00:00:00Z'
    AND f.valid_time <= strftime('%Y-%m-%dT%H:%M:%SZ','now')
)
SELECT model, COUNT(*) AS n,
  ROUND(AVG(ABS(fc-ob)),4) AS mae,
  ROUND(AVG(fc-ob),4) AS bias,
  ROUND(SQRT(AVG((fc-ob)*(fc-ob))),4) AS rmse
FROM scored WHERE rn=1 AND ob IS NOT NULL AND fc IS NOT NULL
GROUP BY model ORDER BY mae;

SELECT '--- YANGON temperature by model (short lead <= 24h) ---';
WITH scored AS (
  SELECT f.model, f.temperature AS fc, o.temperature AS ob,
    ROW_NUMBER() OVER (PARTITION BY f.id
      ORDER BY ABS(strftime('%s', o.observed_at) - strftime('%s', f.valid_time))) AS rn
  FROM weather_forecasts f
  JOIN weather_observations o ON o.location_id = f.location_id
    AND o.observed_at BETWEEN strftime('%Y-%m-%dT%H:%M:%SZ', f.valid_time, '-90 minutes')
                          AND strftime('%Y-%m-%dT%H:%M:%SZ', f.valid_time, '+90 minutes')
  WHERE f.location_id = 1 AND f.forecast_hour <= 24
    AND f.valid_time >= '2026-10-20T00:00:00Z'
    AND f.valid_time <= strftime('%Y-%m-%dT%H:%M:%SZ','now')
)
SELECT model, COUNT(*) AS n,
  ROUND(AVG(ABS(fc-ob)),4) AS mae,
  ROUND(AVG(fc-ob),4) AS bias
FROM scored WHERE rn=1 AND ob IS NOT NULL AND fc IS NOT NULL
GROUP BY model ORDER BY mae;

SELECT '--- WORLDWIDE Tier A temperature by model (all leads) ---';
WITH scored AS (
  SELECT f.model, f.temperature AS fc, o.temperature AS ob,
    ROW_NUMBER() OVER (PARTITION BY f.id
      ORDER BY ABS(strftime('%s', o.observed_at) - strftime('%s', f.valid_time))) AS rn
  FROM weather_forecasts f
  JOIN weather_observations o ON o.location_id = f.location_id
    AND o.observed_at BETWEEN strftime('%Y-%m-%dT%H:%M:%SZ', f.valid_time, '-90 minutes')
                          AND strftime('%Y-%m-%dT%H:%M:%SZ', f.valid_time, '+90 minutes')
  JOIN locations l ON l.id = f.location_id
  WHERE l.collect_every_hours = 1
    AND f.valid_time >= '2026-10-20T00:00:00Z'
    AND f.valid_time <= strftime('%Y-%m-%dT%H:%M:%SZ','now')
)
SELECT model, COUNT(*) AS n,
  ROUND(AVG(ABS(fc-ob)),4) AS mae,
  ROUND(AVG(fc-ob),4) AS bias
FROM scored WHERE rn=1 AND ob IS NOT NULL AND fc IS NOT NULL
GROUP BY model ORDER BY mae;

SELECT '--- WORLDWIDE Tier A rain by model (precip MAE + Brier from precip_probability) ---';
WITH scored AS (
  SELECT f.model, f.precipitation AS fc_precip, f.precip_probability AS fc_prob,
    o.precipitation AS ob_precip,
    ROW_NUMBER() OVER (PARTITION BY f.id
      ORDER BY ABS(strftime('%s', o.observed_at) - strftime('%s', f.valid_time))) AS rn
  FROM weather_forecasts f
  JOIN weather_observations o ON o.location_id = f.location_id
    AND o.observed_at BETWEEN strftime('%Y-%m-%dT%H:%M:%SZ', f.valid_time, '-90 minutes')
                          AND strftime('%Y-%m-%dT%H:%M:%SZ', f.valid_time, '+90 minutes')
  JOIN locations l ON l.id = f.location_id
  WHERE l.collect_every_hours = 1
    AND f.valid_time >= '2026-10-20T00:00:00Z'
    AND f.valid_time <= strftime('%Y-%m-%dT%H:%M:%SZ','now')
)
SELECT model, COUNT(*) AS n,
  ROUND(AVG(ABS(fc_precip-ob_precip)),4) AS precip_mae,
  ROUND(AVG((COALESCE(fc_prob,0.0) - (CASE WHEN ob_precip >= 0.5 THEN 1.0 ELSE 0.0 END)) * (COALESCE(fc_prob,0.0) - (CASE WHEN ob_precip >= 0.5 THEN 1.0 ELSE 0.0 END))),5) AS brier
FROM scored WHERE rn=1 AND ob_precip IS NOT NULL AND fc_precip IS NOT NULL
GROUP BY model ORDER BY brier;
