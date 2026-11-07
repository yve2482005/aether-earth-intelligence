-- Per-location accuracy: champion predictions vs verifying observations.
.timeout 120000
-- One scored pair per (prediction row): nearest observation within 90 min
-- (same tolerance as training). Only champion model versions. Only
-- valid_times that already have observations (<= now).
-- Output: one row per (location, task) with n, MAE, RMSE, bias, MSE
-- (MSE on rain_prob IS the Brier score).
.mode csv
.headers on
.output /home/hatch/workspace/goals/aether-earth-intelligence-platform-build/files/per_location_accuracy_20261106.csv
WITH champ(task, v) AS (
  VALUES ('temperature','weather_temperature_v2026_10_22_006'),
         ('humidity','weather_humidity_v2026_10_22_001'),
         ('rain_prob','weather_rain_prob_v2026_10_20_006'),
         ('precip_amount','weather_precip_amount_v2026_10_20_001'),
         ('wind','weather_wind_v2026_10_20_001'),
         ('pressure','weather_pressure_v2026_10_20_003')
),
scored AS (
  SELECT p.id AS pid, p.location_id, p.task, p.prediction,
    CASE p.task WHEN 'temperature' THEN o.temperature
                WHEN 'humidity' THEN o.humidity
                WHEN 'pressure' THEN o.pressure
                WHEN 'wind' THEN o.wind_speed
                WHEN 'precip_amount' THEN o.precipitation
                WHEN 'rain_prob' THEN (CASE WHEN o.precipitation >= 0.5 THEN 1.0 ELSE 0.0 END)
    END AS obs,
    ROW_NUMBER() OVER (PARTITION BY p.id
      ORDER BY ABS(strftime('%s', o.observed_at) - strftime('%s', p.valid_time))) AS rn
  FROM ml_batch_predictions p
  JOIN champ c ON c.task = p.task AND c.v = p.model_version
  JOIN weather_observations o ON o.location_id = p.location_id
    -- NOTE: never compare ISO 'T...Z' strings against datetime() output
    -- (space-separated): 'T' > ' ' makes every same-date row match.
    -- Format the bounds as 'T...Z' so the string range is correct.
    AND o.observed_at BETWEEN strftime('%Y-%m-%dT%H:%M:%SZ', p.valid_time, '-90 minutes')
                          AND strftime('%Y-%m-%dT%H:%M:%SZ', p.valid_time, '+90 minutes')
  WHERE p.valid_time >= '2026-10-20T00:00:00Z'
    AND p.valid_time <= strftime('%Y-%m-%dT%H:%M:%SZ','now')
)
SELECT l.id AS location_id, l.name, l.country, s.task,
  COUNT(*) AS n,
  ROUND(AVG(ABS(s.prediction - s.obs)), 4) AS mae,
  ROUND(SQRT(AVG((s.prediction - s.obs) * (s.prediction - s.obs))), 4) AS rmse,
  ROUND(AVG(s.prediction - s.obs), 4) AS bias,
  ROUND(AVG((s.prediction - s.obs) * (s.prediction - s.obs)), 5) AS mse
FROM scored s JOIN locations l ON l.id = s.location_id
WHERE s.rn = 1 AND s.obs IS NOT NULL
GROUP BY l.id, s.task
ORDER BY l.id, s.task;
.output stdout
