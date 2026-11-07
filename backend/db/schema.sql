-- AETHER Earth Intelligence — primary schema (SOURCE OF TRUTH)
-- All timestamps: UTC ISO-8601 strings.
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS source_registry (
    source_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    dataset TEXT NOT NULL,
    category TEXT NOT NULL,          -- weather|earthquake|tsunami|air_quality|satellite|radar
    url TEXT,
    api_type TEXT,
    update_frequency TEXT,
    geographic_coverage TEXT,
    resolution TEXT,
    variables TEXT,
    license TEXT,
    attribution TEXT,
    commercial_use TEXT,
    redistribution TEXT,
    rate_limits TEXT,
    auth_required INTEGER DEFAULT 0,
    priority INTEGER DEFAULT 5,
    reliability_score REAL,
    last_success TEXT,
    last_failure TEXT,
    health TEXT DEFAULT 'unknown',   -- ok|degraded|down|unavailable|unknown
    notes TEXT
);

CREATE TABLE IF NOT EXISTS locations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    timezone TEXT,
    country TEXT,
    is_coastal INTEGER DEFAULT 0,
    collect_every_hours INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS weather_forecasts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    location_id INTEGER NOT NULL REFERENCES locations(id),
    source TEXT NOT NULL,            -- open-meteo
    model TEXT NOT NULL,             -- gfs|icon_seamless|ecmwf_ifs|jma_seamless
    forecast_run TEXT NOT NULL,      -- model run timestamp (UTC)
    forecast_hour INTEGER NOT NULL,  -- hours ahead from run
    issued_at TEXT NOT NULL,
    valid_time TEXT NOT NULL,
    temperature REAL, feels_like REAL, humidity REAL, pressure REAL,
    wind_speed REAL, wind_direction REAL, precipitation REAL,
    precip_probability REAL, cloud_cover REAL, visibility REAL, uv REAL, aqi REAL,
    dew_point REAL, wind_gust REAL,
    pm2_5 REAL, pm10 REAL, nitrogen_dioxide REAL, ozone REAL,
    sulphur_dioxide REAL, carbon_monoxide REAL,
    collected_at TEXT NOT NULL,
    UNIQUE(location_id, source, model, forecast_run, forecast_hour)
);

CREATE TABLE IF NOT EXISTS weather_daily (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    location_id INTEGER NOT NULL REFERENCES locations(id),
    source TEXT NOT NULL,            -- open-meteo
    model TEXT NOT NULL,
    date TEXT NOT NULL,              -- YYYY-MM-DD UTC
    sunrise TEXT, sunset TEXT,
    collected_at TEXT NOT NULL,
    UNIQUE(location_id, source, model, date)
);

CREATE TABLE IF NOT EXISTS ocean_forecasts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    location_id INTEGER NOT NULL REFERENCES locations(id),
    source TEXT NOT NULL,            -- open-meteo-marine
    model TEXT NOT NULL,             -- e.g. gfs_wave / icon_wave (as reported)
    valid_time TEXT NOT NULL,
    wave_height REAL,                -- m
    wave_period REAL,                -- s
    wave_direction REAL,             -- deg
    sea_surface_temperature REAL,    -- C
    collected_at TEXT NOT NULL,
    UNIQUE(location_id, source, model, valid_time)
);

CREATE TABLE IF NOT EXISTS weather_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    location_id INTEGER NOT NULL REFERENCES locations(id),
    station TEXT,
    observed_at TEXT NOT NULL,
    temperature REAL, humidity REAL, pressure REAL,
    wind_speed REAL, wind_direction REAL, precipitation REAL,
    source TEXT NOT NULL,
    quality TEXT,                    -- ok|suspect
    collected_at TEXT NOT NULL,
    UNIQUE(location_id, source, observed_at)
);

CREATE TABLE IF NOT EXISTS earthquake_events (
    canonical_event_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    source_event_ids TEXT NOT NULL,  -- JSON array
    origin_time TEXT NOT NULL,
    latitude REAL NOT NULL, longitude REAL NOT NULL,
    depth REAL, magnitude REAL, magnitude_type TEXT,
    region TEXT, status TEXT, review_status TEXT,
    felt_count INTEGER, mmi REAL, alert_level TEXT,
    tsunami_flag INTEGER DEFAULT 0,
    shakemap_available INTEGER DEFAULT 0,
    pager_available INTEGER DEFAULT 0,
    source_url TEXT, update_time TEXT, created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS earthquake_updates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_event_id TEXT NOT NULL REFERENCES earthquake_events(canonical_event_id),
    source_event_id TEXT,
    origin_time TEXT, latitude REAL, longitude REAL, depth REAL,
    magnitude REAL, magnitude_type TEXT, status TEXT,
    update_time TEXT, received_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tsunami_events (
    tsunami_event_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    earthquake_event_id TEXT,
    message_type TEXT NOT NULL,      -- original classification preserved
    severity TEXT,
    affected_region TEXT,
    issue_time TEXT, valid_time TEXT, arrival_time TEXT,
    status TEXT,                     -- active|expired|cancelled|superseded
    source_url TEXT, raw_classification TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tsunami_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tsunami_event_id TEXT REFERENCES tsunami_events(tsunami_event_id),
    message_type TEXT NOT NULL,
    raw_text TEXT, issue_time TEXT,
    received_at TEXT NOT NULL, source_url TEXT,
    version INTEGER               -- v2: message sequence within the event (v1, v2, ...); NULL = pre-versioning
);

CREATE TABLE IF NOT EXISTS earth_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,        -- WEATHER|EARTHQUAKE|TSUNAMI|CYCLONE|FLOOD|HEAT|WILDFIRE|OTHER
    kind TEXT NOT NULL,              -- FORECAST|DETECTION|OBSERVATION|WARNING|RISK
    source TEXT NOT NULL,
    start_time TEXT NOT NULL, end_time TEXT,
    latitude REAL, longitude REAL, region TEXT,
    severity TEXT, confidence TEXT, confidence_reason TEXT,
    status TEXT, source_url TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts (
    alert_id TEXT PRIMARY KEY,
    location_id INTEGER REFERENCES locations(id),
    event_id TEXT,
    type TEXT NOT NULL,              -- WEATHER|EARTHQUAKE|TSUNAMI|CYCLONE|FLOOD|EXTREME_HEAT|EXTREME_WIND
    severity TEXT NOT NULL,          -- info|advisory|watch|warning|critical
    priority INTEGER NOT NULL,       -- 1 official life-safety ... 5 low-confidence analysis
    title TEXT NOT NULL, message TEXT NOT NULL,
    source TEXT NOT NULL,
    kind TEXT NOT NULL,              -- official|verified_observation|system_analysis
    issued_at TEXT NOT NULL, expires_at TEXT,
    status TEXT NOT NULL DEFAULT 'active', -- active|expired|cancelled
    dedup_key TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS model_versions (
    version TEXT PRIMARY KEY,        -- e.g. weather_temperature_v2026_10_05_001
    task TEXT NOT NULL,              -- temperature|rain_prob|precip_amount|wind|pressure|humidity
    dataset_version TEXT, feature_version TEXT, source_versions TEXT,
    training_start TEXT, training_end TEXT,
    validation_start TEXT, validation_end TEXT,
    metrics TEXT,                    -- JSON
    hyperparameters TEXT,            -- JSON
    git_commit TEXT,
    deployed_at TEXT,
    is_champion INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS model_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    location_id INTEGER REFERENCES locations(id),
    variable TEXT NOT NULL, horizon INTEGER NOT NULL,
    model TEXT NOT NULL,
    mae REAL, rmse REAL, brier REAL, crps REAL, calibration_error REAL,
    sample_count INTEGER,
    period_start TEXT, period_end TEXT, model_version TEXT,
    UNIQUE(location_id, variable, horizon, model, model_version)
);

CREATE TABLE IF NOT EXISTS training_runs (
    run_id TEXT PRIMARY KEY,
    task TEXT NOT NULL, dataset_version TEXT, model_version TEXT,
    started_at TEXT, ended_at TEXT, rows INTEGER,
    metrics TEXT, status TEXT NOT NULL,  -- success|failed|rejected
    deployed INTEGER DEFAULT 0, notes TEXT
);

CREATE TABLE IF NOT EXISTS system_health (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    component TEXT NOT NULL,         -- collector:<source_id>|api|db|sheets|drive|ml
    status TEXT NOT NULL,            -- ok|degraded|down|not_configured
    last_success TEXT, last_failure TEXT,
    latency_ms REAL, error_rate REAL, records INTEGER,
    message TEXT, checked_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY, created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS saved_locations (
    user_id TEXT NOT NULL REFERENCES users(id),
    location_id INTEGER NOT NULL REFERENCES locations(id),
    label TEXT,
    PRIMARY KEY (user_id, location_id)
);

CREATE INDEX IF NOT EXISTS idx_wf_loc_time ON weather_forecasts(location_id, valid_time);
CREATE INDEX IF NOT EXISTS idx_ocean_loc_time ON ocean_forecasts(location_id, valid_time);
CREATE INDEX IF NOT EXISTS idx_wdaily_loc ON weather_daily(location_id, date);
CREATE INDEX IF NOT EXISTS idx_wo_loc_time ON weather_observations(location_id, observed_at);
CREATE INDEX IF NOT EXISTS idx_eq_time ON earthquake_events(origin_time);
CREATE INDEX IF NOT EXISTS idx_ee_time ON earth_events(start_time);
CREATE INDEX IF NOT EXISTS idx_alerts_status ON alerts(status, issued_at);

-- ================= v2 additions (additive only; never alter tables above) ===

CREATE TABLE IF NOT EXISTS alert_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id TEXT NOT NULL REFERENCES alerts(alert_id),
    revision INTEGER NOT NULL,
    title TEXT, message TEXT, severity TEXT, status TEXT,
    changed_by TEXT, changed_at TEXT NOT NULL, reason TEXT,
    UNIQUE(alert_id, revision)
);

CREATE TABLE IF NOT EXISTS notification_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id TEXT REFERENCES alerts(alert_id),
    channel TEXT NOT NULL,               -- push|webhook|sms|email
    recipient TEXT,
    title TEXT, body TEXT,
    status TEXT NOT NULL DEFAULT 'queued', -- queued|sent|failed|skipped
    error TEXT,
    created_at TEXT NOT NULL, sent_at TEXT
);

CREATE TABLE IF NOT EXISTS notification_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    notification_id INTEGER NOT NULL REFERENCES notification_history(id),
    channel TEXT NOT NULL,
    provider_message_id TEXT,
    delivered_at TEXT,
    status TEXT NOT NULL,                -- delivered|failed|unknown
    detail TEXT
);

CREATE TABLE IF NOT EXISTS incidents (
    incident_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    component TEXT,
    severity TEXT,                       -- info|minor|major|critical
    status TEXT NOT NULL DEFAULT 'open', -- open|investigating|mitigated|resolved|closed
    started_at TEXT NOT NULL, ended_at TEXT,
    summary TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS devices (
    device_id TEXT PRIMARY KEY,
    user_id TEXT REFERENCES users(id),
    platform TEXT NOT NULL,              -- android|web|ios
    push_token TEXT,
    app_version TEXT,
    last_seen TEXT, created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alert_preferences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT REFERENCES users(id),
    device_id TEXT REFERENCES devices(device_id),
    channel TEXT NOT NULL,
    min_severity TEXT DEFAULT 'advisory', -- info|advisory|watch|warning|critical
    alert_types TEXT,                    -- JSON array of alert type strings
    enabled INTEGER DEFAULT 1,
    updated_at TEXT NOT NULL,
    UNIQUE(user_id, device_id, channel)
);

CREATE TABLE IF NOT EXISTS data_quality (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    component TEXT NOT NULL,             -- collector:<source_id>|validate-data|api
    check_name TEXT NOT NULL,
    status TEXT NOT NULL,                -- pass|fail|warn
    details TEXT,                        -- JSON with counts / samples
    checked_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingestion_runs (
    run_id TEXT PRIMARY KEY,
    component TEXT NOT NULL,
    started_at TEXT NOT NULL, ended_at TEXT,
    status TEXT NOT NULL,                -- running|success|failed|partial
    rows_written INTEGER DEFAULT 0,
    error TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dq_comp_time ON data_quality(component, checked_at);
CREATE INDEX IF NOT EXISTS idx_ir_comp_time ON ingestion_runs(component, started_at);
CREATE INDEX IF NOT EXISTS idx_nh_alert ON notification_history(alert_id, created_at);
