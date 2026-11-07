/* AETHER Home — dashboard (v2).
   Current conditions: FORECAST (forecast_now) + OBSERVATION (observation).
   Response shape (verified): {location, forecast_now:{...}, observation:{...}, provenance}.
   The old build read metric keys at the top level and rendered dashes; every
   metric below is mapped against the REAL shape. Missing values render as
   "unavailable", never invented.
   Banner honesty: a failed secondary fetch must not raise the global stale
   banner when the primary data rendered fine — the banner is hidden again
   when /weather/current succeeded. */
(function () {
  'use strict';
  const A = window.AETHER;
  const t = (k, v) => A.i18n.t(k, v);

  function trendArrow(tr) {
    tr = String(tr || 'steady').toLowerCase();
    if (tr === 'rising') return '<span aria-hidden="true">▲</span> ' + A.esc(t('home.trend.rising'));
    if (tr === 'falling') return '<span aria-hidden="true">▼</span> ' + A.esc(t('home.trend.falling'));
    return '<span aria-hidden="true">➖</span> ' + A.esc(t('home.trend.steady'));
  }

  function confidenceBlock(conf) {
    if (!conf) return '';
    const pct = conf.percent != null ? conf.percent : (conf.level === 'high' ? 85 : conf.level === 'medium' ? 65 : 40);
    const reasons = Array.isArray(conf.reasons) ? conf.reasons : (Array.isArray(conf.why) ? conf.why : []);
    const p = Math.max(0, Math.min(100, Math.round(pct)));
    return '<div class="conf"><div class="conf-ring" style="--p:' + p + '" role="img" aria-label="' +
      A.esc(t('home.confidence') + ' ' + p + '%') + '"><b>' + p + '%</b></div>' +
      '<span>' + A.esc(t('home.confidence')) + '</span></div>' +
      (reasons.length ? '<details class="why"><summary>' + A.esc(t('home.confidence.why')) + '</summary><ul>' +
        reasons.map(r => '<li>' + A.esc(r) + '</li>').join('') + '</ul></details>' : '');
  }

  function modelsBlock(models) {
    if (!Array.isArray(models) || !models.length) return '';
    return '<h3>' + A.esc(t('home.models')) + '</h3><div class="models" role="list">' +
      models.map(m => {
        const name = A.esc(A.pick(m, ['model', 'name']) || '?');
        const agrees = !!A.pick(m, ['agrees', 'agreement']);
        return '<span class="model-chip ' + (agrees ? 'agree' : 'disagree') + '" role="listitem">' + name + '</span>';
      }).join('') + '</div>';
  }

  function stat(k, v) {
    return '<div class="stat"><div class="v">' + v + '</div><div class="k">' + A.esc(k) + '</div></div>';
  }

  function fmtVal(v, fmt) {
    return v != null && !isNaN(v) ? fmt(v) : '—';
  }

  /* Astronomical moon phase — local computation, no API. Labeled ESTIMATED. */
  function moonPhase(date) {
    const synodic = 29.53058867;
    const knownNew = Date.UTC(2000, 0, 6, 18, 14) / 86400000;
    const days = date.getTime() / 86400000;
    let age = (days - knownNew) % synodic;
    if (age < 0) age += synodic;
    const idx = Math.floor(((age / synodic) * 8) + 0.5) % 8;
    const keys = ['new', 'waxing_crescent', 'first_quarter', 'waxing_gibbous',
      'full', 'waning_gibbous', 'last_quarter', 'waning_crescent'];
    const icons = ['🌑', '🌒', '🌓', '🌔', '🌕', '🌖', '🌗', '🌘'];
    return { age: Math.round(age * 10) / 10, idx, key: keys[idx], icon: icons[idx] };
  }

  /* Hourly temperature curve — SVG polyline over the .temp-curve CSS hook.
     Purely presentational: null gaps are skipped, never interpolated. */
  function tempCurve(hours) {
    const pts = hours.slice(0, 24).map(x => {
      const v = A.pick(x, ['temperature', 'temp']);
      return (v != null && !isNaN(v)) ? Number(v) : null;
    });
    const vals = pts.filter(v => v != null);
    if (vals.length < 2) return '';
    const W = 480, H = 64, P = 6;
    const mn = Math.min.apply(null, vals), mx = Math.max.apply(null, vals);
    const span = (mx - mn) || 1;
    let d = '';
    pts.forEach((v, i) => {
      if (v == null) return;
      const px = P + (i * (W - 2 * P)) / (pts.length - 1);
      const py = H - P - ((v - mn) / span) * (H - 2 * P);
      d += (d ? 'L' : 'M') + px.toFixed(1) + ' ' + py.toFixed(1);
    });
    return '<svg class="temp-curve" viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none" role="img" aria-label="' +
      A.esc(t('home.hourly')) + '"><path d="' + d + '"/></svg>';
  }

  function pollutantsBlock(fn) {
    const defs = [
      ['pm2_5', 'hx.pm25'], ['pm10', 'hx.pm10'], ['nitrogen_dioxide', 'hx.no2'],
      ['ozone', 'hx.o3'], ['sulphur_dioxide', 'hx.so2'], ['carbon_monoxide', 'hx.co']
    ];
    const anyPresent = defs.some(([k]) => fn[k] != null);
    let html = '<h3>' + A.esc(t('hx.pollutants')) + '</h3>';
    if (!anyPresent) {
      html += '<p class="map-note">' + A.esc(t('hx.pollutants_unavailable')) + '</p>';
      return html;
    }
    html += '<div class="grid3">' + defs.map(([k, label]) =>
      stat(t(label), fmtVal(fn[k], v => Math.round(v * 10) / 10 + ' ' + t('hx.unit_ugm3')))
    ).join('') + '</div>';
    return html;
  }

  function sunMoonBlock(sm) {
    const moon = moonPhase(new Date());
    let html = '<h3>' + A.esc(t('hx.sun_moon')) + '</h3><div class="grid3">';
    html += stat(t('hx.sunrise'), sm && sm.sunrise ? A.esc(A.fmtDateTime(sm.sunrise, { dateStyle: undefined, timeStyle: 'short' })) : '—');
    html += stat(t('hx.sunset'), sm && sm.sunset ? A.esc(A.fmtDateTime(sm.sunset, { dateStyle: undefined, timeStyle: 'short' })) : '—');
    html += '<div class="stat"><div class="v"><span aria-hidden="true">' + moon.icon + '</span> ' +
      A.esc(t('hx.moon.' + moon.key)) + '</div><div class="k">' + A.esc(t('hx.moon')) + ' ' + A.kindTag('estimated') + '</div></div>';
    html += '</div>';
    if (sm && sm.note) html += '<p class="map-note">' + A.esc(sm.note) + '</p>';
    return html;
  }

  function nowcastBlock(nc) {
    if (!nc) return '';
    let html = '<div class="card"><div style="display:flex;justify-content:space-between;align-items:center;gap:8px">' +
      '<h3>' + A.esc(t('hx.nowcast')) + '</h3>' + A.kindTag(nc.kind || 'nowcast') + '</div>';
    if (nc.status === 'nowcast_unavailable' && nc.message) {
      html += '<div class="note" role="note">ℹ️ ' + A.esc(nc.message) + '</div>';
    }
    const wins = Array.isArray(nc.windows) ? nc.windows : [];
    if (wins.length) {
      html += '<div class="grid3">' + wins.map(w => {
        const vals = w.values || {};
        const tmp = vals.temperature;
        return '<div class="stat"><div class="v">' + A.esc(A.fmtTemp(tmp)) +
          '</div><div class="k">' + A.esc(w.window + ' ' + t('map.hour_unit') + ' · ' + String(w.kind || '')) + '</div></div>';
      }).join('') + '</div>';
      html += '<p class="map-note">' + A.esc(t('hx.nowcast_note')) + '</p>';
    }
    html += A.updatedLine(nc, {}) + '</div>';
    return html;
  }

  async function render(host) {
    const loc = A.store.active;
    host.innerHTML = '<div class="card"><div class="skeleton" style="height:220px"></div></div>';
    const q = '?lat=' + loc.lat + '&lon=' + loc.lon;
    const [cur, hourly, daily, sunmoon, nowcast] = await Promise.all([
      A.api.request('/weather/current' + q),
      A.api.request('/weather/hourly' + q),
      A.api.request('/weather/daily' + q),
      A.api.request('/engines/sunmoon' + q),
      A.api.request('/nowcast' + q)
    ]);

    // Banner honesty: only the primary fetch failing is a page-level alarm.
    if (cur.ok && cur.data) A.banner.hideStale();

    if (!cur.data && !hourly.data && !daily.data) {
      host.innerHTML = '<div class="card">' + A.emptyState('common.unavailable.title', 'common.unavailable.desc') + '</div>';
      return;
    }

    let html = '<div class="card"><h2>' + A.esc(t('home.title')) + ' · ' + A.esc(loc.name) + '</h2>';

    // ---- Current conditions: forecast_now (FORECAST) + observation (OBSERVATION)
    const d = cur.data || {};
    const fn = d.forecast_now || {};
    const ob = d.observation || {};
    const temp = fn.temperature != null ? fn.temperature : ob.temperature;
    const feels = fn.feels_like != null ? fn.feels_like : ob.feels_like;
    const hum = fn.humidity != null ? fn.humidity : ob.humidity;
    const wind = fn.wind_speed != null ? fn.wind_speed : ob.wind_speed;
    const rain = fn.precip_probability;
    const aqi = fn.aqi;
    const uv = fn.uv;
    const dew = fn.dew_point;
    const gust = fn.wind_gust;
    const vis = fn.visibility;
    const pressure = fn.pressure != null ? fn.pressure : ob.pressure;
    const cloud = fn.cloud_cover;
    const trend = fn.trend || (fn.trends && fn.trends.temperature);

    html += '<div style="display:flex;justify-content:space-between;align-items:center;gap:8px">' +
      '<h3>' + A.esc(t('home.conditions')) + '</h3>' +
      '<span>' + A.kindTag('forecast') + ' ' + A.kindTag('observation') + '</span></div>';
    html += '<div style="display:flex;gap:16px;align-items:baseline;flex-wrap:wrap">' +
      '<span class="big-temp">' + A.esc(A.fmtTemp(temp)) + '</span>' +
      '<span>' + A.esc(t('home.feels_like') + ': ' + A.fmtTemp(feels)) + '</span></div>';
    html += '<div class="grid3">' +
      stat(t('home.rain'), fmtVal(rain, v => Math.round(v) + '%')) +
      stat(t('home.wind'), A.fmtSpeed(wind)) +
      stat(t('hx.wind_gust'), A.fmtSpeed(gust)) +
      stat(t('home.humidity'), fmtVal(hum, v => Math.round(v) + '%')) +
      stat(t('hx.dew_point'), fmtVal(dew, v => A.fmtTemp(v))) +
      stat(t('hx.visibility'), fmtVal(vis, v => (v >= 1000 ? (v / 1000).toFixed(1) + ' km' : Math.round(v) + ' m'))) +
      stat(t('hx.uv_index'), fmtVal(uv, v => String(Math.round(v * 10) / 10))) +
      stat(t('hx.pressure'), fmtVal(pressure, v => Math.round(v) + ' hPa')) +
      stat(t('hx.cloud'), fmtVal(cloud, v => Math.round(v) + '%')) +
      stat(t('home.aqi'), aqi != null ? A.esc(String(Math.round(aqi))) : '—') +
      stat(t('home.trend'), trendArrow(trend)) +
      stat(t('home.updated'), A.esc(A.minutesSince(A.provOf(d).updated_at) != null ? t('common.updated_ago', { min: A.minutesSince(A.provOf(d).updated_at) }) : '—')) +
      '</div>';
    if (fn.model) html += '<p class="map-note">' + A.esc(t('hx.model') + ': ' + fn.model) + '</p>';
    html += pollutantsBlock(fn);
    html += sunMoonBlock(sunmoon.data && sunmoon.data.status !== 'unavailable' ? sunmoon.data : null);
    html += confidenceBlock(fn.confidence);
    html += modelsBlock(A.pick(fn, ['model_agreement', 'models']));
    html += A.updatedLine(d, cur);
    html += '</div>';

    // ---- Nowcast (0–120 min): NOWCAST vs NWP FORECAST labeled per window
    html += nowcastBlock(nowcast.data);

    // ---- Hourly = FORECAST (real keys: items[].valid_time / temperature / precipitation_probability)
    const h = hourly.data || {};
    const hours = h.items || A.pick(h, ['hours', 'hourly', 'data']) || [];
    if (Array.isArray(hours) && hours.length) {
      html += '<div class="card"><div style="display:flex;justify-content:space-between;align-items:center;gap:8px">' +
        '<h3>' + A.esc(t('home.hourly')) + '</h3>' + A.kindTag('forecast') + '</div>' +
        tempCurve(hours) +
        '<div class="grid3">' + hours.slice(0, 6).map(x => {
          const tt = A.pick(x, ['temperature', 'temp']);
          const rp = A.pick(x, ['rain_probability', 'precipitation_probability', 'precip_probability']);
          const tm = A.pick(x, ['valid_time', 'time', 'datetime']);
          return stat(tm ? A.esc(A.fmtDateTime(tm, { dateStyle: undefined, timeStyle: 'short' })) : '—',
            A.esc(A.fmtTemp(tt)) + (rp != null ? ' · ' + Math.round(rp) + '%' : ''));
        }).join('') + '</div>' + A.updatedLine(h, hourly) + '</div>';
    }

    // ---- Daily = FORECAST (real keys: items[].day / tmin / tmax)
    const dd = daily.data || {};
    const days = dd.items || A.pick(dd, ['days', 'daily', 'data']) || [];
    if (Array.isArray(days) && days.length) {
      html += '<div class="card"><div style="display:flex;justify-content:space-between;align-items:center;gap:8px">' +
        '<h3>' + A.esc(t('home.daily')) + '</h3>' + A.kindTag('forecast') + '</div><dl class="facts">' +
        days.slice(0, 5).map(x => {
          const dt = A.pick(x, ['day', 'date', 'time']);
          const mx = A.pick(x, ['tmax', 'temp_max', 'temperature_max', 'max_temp']);
          const mn = A.pick(x, ['tmin', 'temp_min', 'temperature_min', 'min_temp']);
          const sr = A.pick(x, ['sunrise']), ss = A.pick(x, ['sunset']);
          return '<dt>' + A.esc(dt ? A.fmtDateTime(dt, { timeStyle: undefined, dateStyle: 'medium' }) : '—') + '</dt>' +
            '<dd>' + A.esc(A.fmtTemp(mn) + ' / ' + A.fmtTemp(mx)) +
            (sr || ss ? '<br><span class="map-note">' + A.esc(t('hx.sunrise') + ': ' + (sr ? A.fmtDateTime(sr, { dateStyle: undefined, timeStyle: 'short' }) : '—') +
              ' · ' + t('hx.sunset') + ': ' + (ss ? A.fmtDateTime(ss, { dateStyle: undefined, timeStyle: 'short' }) : '—')) + '</span>' : '') +
            '</dd>';
        }).join('') + '</dl>' + A.updatedLine(dd, daily) + '</div>';
    }

    host.innerHTML = html;
    A.i18n.applyStatic(host);
  }

  A.views.home = { render };
})();
