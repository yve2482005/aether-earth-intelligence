/* AETHER Map (v2) — Leaflet.
   Layers (only where real free data exists):
     weather    — current-condition markers per saved location (/v1/weather/current)
     air quality— AQI + pollutant markers per saved location (same endpoint)
     ocean      — wave markers from /v1/engines/ocean (real marine API data)
     quakes     — USGS detections, sized by magnitude, colored by depth/age
     tsunami    — official messages only, provenance always shown
     satellite/radar — HONEST "coverage unavailable" states; no fake imagery,
                       per spec §§25-26, 115. Shown greyed-out, never rendered.
   Per-layer legend carries unit + source + timestamp (§91).
   Quake timeline: play/pause animation stepping through origin times.
   Degrades honestly when Leaflet CDN or the API is unreachable. */
(function () {
  'use strict';
  const A = window.AETHER;
  const t = (k, v) => A.i18n.t(k, v);
  let map = null, layerGroups = {}, tileLight = null, tileDark = null;
  let quakeList = [], quakeAnim = null, sliderHour = 0;
  const layerMeta = {};   // layer -> {unit, source, updated}
  const layerOn = { weather: true, aqi: false, ocean: false, quakes: true, tsunami: true };

  const TILES = {
    light: 'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
    dark: 'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png'
  };

  function ensureMap(host) {
    if (typeof L === 'undefined') {
      host.innerHTML = '<div class="card">' + A.emptyState('map.unavailable', 'map.no_leaflet') + '</div>';
      return false;
    }
    if (map) { setTimeout(() => map.invalidateSize(), 50); return true; }
    const loc = A.store.active;
    map = L.map('map', { worldCopyJump: true }).setView([loc.lat, loc.lon], 5);
    tileLight = L.tileLayer(TILES.light, { maxZoom: 18, attribution: '© OpenStreetMap contributors' });
    tileDark = L.tileLayer(TILES.dark, { maxZoom: 18, attribution: '© OpenStreetMap contributors © CARTO' });
    (A.settings.get('theme') === 'dark' ? tileDark : tileLight).addTo(map);
    ['weather', 'aqi', 'ocean', 'quakes', 'tsunami'].forEach(name => {
      layerGroups[name] = (name === 'quakes' && typeof L.markerClusterGroup === 'function')
        ? L.markerClusterGroup({ maxClusterRadius: 50 })
        : L.layerGroup();
      if (layerOn[name]) layerGroups[name].addTo(map);
    });
    map.on('mousemove', e => {
      const el = document.getElementById('map-coords');
      if (el) el.textContent = e.latlng.lat.toFixed(4) + ', ' + e.latlng.lng.toFixed(4);
    });
    return true;
  }

  A.views.map = {
    retheme() {
      if (!map) return;
      const dark = A.settings.get('theme') === 'dark';
      if (dark && map.hasLayer(tileLight)) { map.removeLayer(tileLight); tileDark.addTo(map); }
      if (!dark && map.hasLayer(tileDark)) { map.removeLayer(tileDark); tileLight.addTo(map); }
    },

    async render(host) {
      if (map) { try { map.remove(); } catch (e) {} map = null; layerGroups = {}; }
      stopQuakeAnim();
      const loc = A.store.active;
      host.innerHTML =
        '<div class="card"><h2>' + A.esc(t('map.title')) + '</h2>' +
        '<div class="map-tools">' +
          '<div class="grow"><input type="text" id="map-search" data-i18n-ph="map.search" aria-label="' + A.esc(t('map.search')) + '" list="gaz-list" autocomplete="off">' +
          '<datalist id="gaz-list"></datalist></div>' +
          '<button type="button" class="btn btn-small" id="map-style-toggle">' + A.esc(t('map.theme')) + '</button>' +
        '</div>' +
        '<div id="map" role="application" aria-label="' + A.esc(t('map.title')) + '"></div>' +
        '<div class="map-tools"><span id="map-coords" class="map-note" aria-live="polite"></span></div>' +
        '<div class="map-tools" id="layer-panel" role="group" aria-label="' + A.esc(t('mapx.layers')) + '"></div>' +
        '<div class="map-tools"><label class="grow" style="font-size:13px;font-weight:700">' +
          A.esc(t('map.timeline')) + ': <span id="map-hour-label">0' + A.esc(t('map.hour_unit')) + '</span>' +
          '<input type="range" class="timeline-slider" id="map-hour" min="0" max="23" value="0" step="1" aria-label="' + A.esc(t('map.timeline')) + '"></label>' +
          '<button type="button" class="btn btn-small" id="quake-play">' + A.esc(t('mapx.play')) + '</button>' +
          '<select id="quake-speed" aria-label="' + A.esc(t('mapx.speed')) + '">' +
          '<option value="1">1×</option><option value="2">2×</option><option value="4">4×</option></select>' +
          '<span id="quake-day-label" class="map-note"></span></div>' +
        '<div class="legend" id="map-legend"></div>' +
        '<p class="map-note" id="map-prov"></p></div>';
      A.i18n.applyStatic(host);
      if (!ensureMap(host)) return;

      // search (existing)
      const input = host.querySelector('#map-search');
      const dl = host.querySelector('#gaz-list');
      input.addEventListener('input', () => {
        const hits = A.gazetteerSearch(input.value);
        dl.innerHTML = hits.map(g => '<option value="' + A.esc(g.n) + ', ' + A.esc(g.c) + '">').join('');
        input._hits = hits;
      });
      input.addEventListener('change', () => {
        const hits = input._hits || [];
        const g = hits.find(h => (h.n + ', ' + h.c) === input.value) || hits[0];
        if (g) { map.setView([g.lat, g.lon], 7); L.marker([g.lat, g.lon]).addTo(map).bindPopup(A.esc(g.n)).openPopup(); }
      });
      host.querySelector('#map-style-toggle').addEventListener('click', () => {
        A.settings.set('theme', A.settings.get('theme') === 'dark' ? 'light' : 'dark');
        A.applyChrome(); A.views.map.retheme();
      });
      host.querySelector('#map-hour').addEventListener('input', e => {
        sliderHour = +e.target.value;
        host.querySelector('#map-hour-label').textContent = sliderHour + t('map.hour_unit');
        drawWeather();
      });
      host.querySelector('#quake-play').addEventListener('click', () => toggleQuakeAnim(host));
      host.querySelector('#quake-speed').addEventListener('change', () => { if (quakeAnim) { stopQuakeAnim(); startQuakeAnim(host); } });

      buildLayerPanel(host);
      await Promise.all([loadWeather(), loadAqi(), loadOcean(), loadQuakes(), loadTsunami(host)]);
      drawLegend(host);
      const prov = host.querySelector('#map-prov');
      if (prov) prov.innerHTML = Object.keys(layerMeta).map(k =>
        A.esc(t('mapx.layer.' + k) + ': ' + (layerMeta[k].source || '—') +
          (layerMeta[k].updated ? ' · ' + A.fmtDateTime(layerMeta[k].updated) : ''))).join('<br>');
    }
  };

  /* ---------- layer toggles ---------- */
  const LAYER_DEFS = [
    ['weather', true], ['aqi', false], ['ocean', false], ['quakes', true], ['tsunami', true]
  ];

  function buildLayerPanel(host) {
    const panel = host.querySelector('#layer-panel');
    let html = '<strong>' + A.esc(t('mapx.layers')) + ':</strong> ';
    html += LAYER_DEFS.map(([name]) =>
      '<label class="chip"><input type="checkbox" data-layer="' + name + '"' +
      (layerOn[name] ? ' checked' : '') + '> ' + A.esc(t('mapx.layer.' + name)) + '</label>'
    ).join(' ');
    // satellite / radar: honest unavailable states — selectable never, rendered never
    html += '<span class="chip off" title="' + A.esc(t('mapx.unavailable_note')) + '">' +
      A.esc(t('mapx.layer.satellite')) + ' (' + A.esc(t('mapx.unavailable')) + ')</span> ' +
      '<span class="chip off" title="' + A.esc(t('mapx.unavailable_note')) + '">' +
      A.esc(t('mapx.layer.radar')) + ' (' + A.esc(t('mapx.unavailable')) + ')</span>';
    panel.innerHTML = html;
    panel.querySelectorAll('[data-layer]').forEach(cb =>
      cb.addEventListener('change', () => {
        const name = cb.getAttribute('data-layer');
        layerOn[name] = cb.checked;
        if (!map || !layerGroups[name]) return;
        if (cb.checked) layerGroups[name].addTo(map); else map.removeLayer(layerGroups[name]);
      }));
  }

  function drawLegend(host) {
    const lg = host.querySelector('#map-legend');
    if (!lg) return;
    let html = '<strong>' + A.esc(t('map.legend')) + '</strong>';
    html += '<div class="row"><span class="dot" style="background:#fee5d9"></span>' + A.esc(t('map.legend.shallow')) +
      '<span class="dot" style="background:#fb6a4a"></span><span class="dot" style="background:#a50f15"></span>' + A.esc(t('map.legend.deep')) + '</div>';
    html += '<div class="row">○ ' + A.esc(t('map.legend.mag')) + ' — ' + A.esc(t('map.legend.note')) + '</div>';
    // per-layer unit + source + timestamp (§91)
    Object.keys(layerMeta).forEach(k => {
      const m = layerMeta[k];
      html += '<div class="row"><strong>' + A.esc(t('mapx.layer.' + k)) + '</strong>: ' +
        A.esc(m.unit || '—') + ' · ' + A.esc(t('common.source')) + ': ' + A.esc(m.source || '—') +
        (m.updated ? ' · ' + A.esc(A.fmtDateTime(m.updated)) : '') + '</div>';
    });
    lg.innerHTML = html;
  }

  function setMeta(layer, unit, source, updated) {
    layerMeta[layer] = { unit, source, updated };
  }

  /* ---------- weather markers: real collected data per saved location ---------- */
  let weatherHourly = null;

  async function loadWeather() {
    const locs = A.store.all.slice(0, 8);
    const results = await Promise.all(locs.map(l =>
      A.api.request('/weather/current?lat=' + l.lat + '&lon=' + l.lon)
        .then(r => ({ loc: l, data: r.data })).catch(() => ({ loc: l, data: null }))));
    if (!map || !layerGroups.weather) return;
    layerGroups.weather.clearLayers();
    let latest = null;
    results.forEach(({ loc: l, data }) => {
      const fn = (data && data.forecast_now) || {};
      const temp = fn.temperature;
      if (temp == null) return;
      const icon = L.divIcon({ className: '', html: '<div style="background:rgba(255,255,255,.92);border:1px solid #999;border-radius:8px;padding:2px 6px;font-size:11px;font-weight:700;white-space:nowrap">' + A.esc(A.fmtTemp(temp)) + '</div>', iconSize: null });
      L.marker([l.lat, l.lon], { icon }).addTo(layerGroups.weather)
        .bindPopup('<strong>' + A.esc(l.name) + '</strong><br>' + A.esc(A.fmtTemp(temp)) +
          '<br>' + A.esc(t('home.rain')) + ': ' + (fn.precip_probability != null ? Math.round(fn.precip_probability) + '%' : '—') +
          '<br>' + A.esc(t('home.wind')) + ': ' + A.esc(A.fmtSpeed(fn.wind_speed)) +
          '<br>' + A.kindTag('forecast'));
      const upd = data && data.provenance && data.provenance.updated_at;
      if (upd && (!latest || upd > latest)) latest = upd;
    });
    setMeta('weather', '°C / % / ' + t('map.hour_unit'), 'open-meteo', latest);
    // active-location hourly marker for the selected forecast hour
    const hr = await A.api.request('/weather/current?lat=' + A.store.active.lat + '&lon=' + A.store.active.lon).catch(() => null);
    weatherHourly = null;
    const hres = await A.api.request('/weather/hourly?lat=' + A.store.active.lat + '&lon=' + A.store.active.lon).catch(() => null);
    if (hres && hres.data) weatherHourly = hres.data;
    drawWeather();
  }

  function drawWeather() {
    if (!map || !layerGroups.weather) return;
    // remove previous hour ring (tagged) without clearing location markers
    layerGroups.weather.eachLayer(l => { if (l._hourRing) layerGroups.weather.removeLayer(l); });
    const hours = weatherHourly ? (weatherHourly.items || A.pick(weatherHourly, ['hours', 'hourly', 'data']) || []) : [];
    const loc = A.store.active;
    if (Array.isArray(hours) && hours[sliderHour]) {
      const x = hours[sliderHour];
      const temp = A.pick(x, ['temperature', 'temp']);
      const ring = L.circleMarker([loc.lat, loc.lon], { radius: 14, color: '#0b5fa5', weight: 3, fillOpacity: 0.15 });
      ring._hourRing = true;
      ring.addTo(layerGroups.weather)
        .bindPopup('<strong>' + A.esc(loc.name) + '</strong> +' + sliderHour + t('map.hour_unit') + '<br>' +
          A.esc(A.fmtTemp(temp)) + '<br>' + A.kindTag('forecast')).openPopup();
    }
  }

  /* ---------- air quality layer ---------- */
  function aqiColor(aqi) {
    if (aqi == null) return '#7f7f7f';
    if (aqi <= 50) return '#2ca02c'; if (aqi <= 100) return '#ffbd0b';
    if (aqi <= 150) return '#ff7f0e'; if (aqi <= 200) return '#d62728';
    if (aqi <= 300) return '#9467bd'; return '#7f0000';
  }

  async function loadAqi() {
    const locs = A.store.all.slice(0, 8);
    const results = await Promise.all(locs.map(l =>
      A.api.request('/weather/current?lat=' + l.lat + '&lon=' + l.lon)
        .then(r => ({ loc: l, data: r.data })).catch(() => ({ loc: l, data: null }))));
    if (!map || !layerGroups.aqi) return;
    layerGroups.aqi.clearLayers();
    let latest = null;
    results.forEach(({ loc: l, data }) => {
      const fn = (data && data.forecast_now) || {};
      if (fn.aqi == null) return;
      L.circleMarker([l.lat, l.lon], { radius: 12, color: '#333', weight: 1, fillColor: aqiColor(fn.aqi), fillOpacity: 0.85 })
        .addTo(layerGroups.aqi)
        .bindPopup('<strong>' + A.esc(l.name) + '</strong><br>' + A.esc(t('home.aqi')) + ': ' + A.esc(String(Math.round(fn.aqi))) +
          (fn.pm2_5 != null ? '<br>PM2.5: ' + A.esc(String(Math.round(fn.pm2_5 * 10) / 10)) + ' µg/m³' : '') +
          '<br>' + A.kindTag('forecast'));
      const upd = data && data.provenance && data.provenance.updated_at;
      if (upd && (!latest || upd > latest)) latest = upd;
    });
    setMeta('aqi', 'US AQI / µg/m³', 'open-meteo (CAMS)', latest);
  }

  /* ---------- ocean layer (real marine data) ---------- */
  async function loadOcean() {
    const loc = A.store.active;
    const res = await A.api.request('/engines/ocean?lat=' + loc.lat + '&lon=' + loc.lon + '&hours=48').catch(() => null);
    if (!map || !layerGroups.ocean) return;
    layerGroups.ocean.clearLayers();
    const data = res && res.data;
    if (!data || data.status === 'unavailable' || !Array.isArray(data.items) || !data.items.length) {
      setMeta('ocean', 'm / s', 'open-meteo-marine', null);
      return;
    }
    const items = data.items.filter((_, i) => i % 6 === 0).slice(0, 8);
    items.forEach(it => {
      const wh = it.wave_height;
      if (wh == null) return;
      const icon = L.divIcon({ className: '', html: '<div style="background:rgba(11,95,165,.9);color:#fff;border-radius:8px;padding:2px 6px;font-size:11px;font-weight:700;white-space:nowrap">🌊 ' + A.esc(String(Math.round(wh * 10) / 10)) + 'm</div>', iconSize: null });
      L.marker([loc.lat, loc.lon], { icon }).addTo(layerGroups.ocean)
        .bindPopup('<strong>' + A.esc(loc.name) + '</strong><br>' +
          A.esc(A.fmtDateTime(it.valid_time, { dateStyle: undefined, timeStyle: 'short' })) + '<br>' +
          '🌊 ' + A.esc(String(wh)) + ' m · ' + A.esc(it.wave_period != null ? String(it.wave_period) + ' s' : '—') +
          (it.sea_surface_temperature != null ? '<br>SST ' + A.esc(A.fmtTemp(it.sea_surface_temperature)) : '') +
          '<br>' + A.kindTag('forecast'));
    });
    const prov = data.provenance || {};
    setMeta('ocean', 'm (wave height) / s (period)', 'open-meteo-marine', prov.updated_at);
  }

  /* ---------- earthquakes + timeline animation ---------- */
  function depthColor(depthKm, ageMin) {
    if (ageMin != null && ageMin < 60) return '#d62728';
    if (depthKm == null) return '#7f7f7f';
    if (depthKm < 70) return '#fee5d9';
    if (depthKm < 300) return '#fb6a4a';
    return '#a50f15';
  }

  async function loadQuakes() {
    const res = await A.api.request('/earthquakes');
    if (!res.data || !map || !layerGroups.quakes) return;
    quakeList = A.pick(res.data, ['earthquakes', 'events', 'data']) || [];
    if (!Array.isArray(quakeList)) quakeList = [];
    drawQuakes(Infinity);
    const prov = res.data.provenance || {};
    setMeta('quakes', 'M (magnitude) / km (depth)', (prov.sources || []).join(', ') || 'usgs', prov.updated_at);
  }

  function drawQuakes(cutoffMs) {
    if (!map || !layerGroups.quakes) return;
    layerGroups.quakes.clearLayers();
    quakeList.slice(0, 500).forEach(q => {
      const tm = A.pick(q, ['time', 'occurred_at', 'origin_time']);
      if (tm && new Date(tm).getTime() > cutoffMs) return;
      const lat = A.pick(q, ['lat', 'latitude']), lon = A.pick(q, ['lon', 'longitude']);
      const mag = A.pick(q, ['magnitude', 'mag']);
      if (lat == null || lon == null) return;
      const depth = A.pick(q, ['depth_km', 'depth']);
      const ageMin = A.minutesSince(tm);
      const r = 6 + (mag != null ? Math.min(18, mag * 3) : 0);
      L.circleMarker([lat, lon], {
        radius: r, color: '#333', weight: 1,
        fillColor: depthColor(depth, ageMin), fillOpacity: 0.85
      }).addTo(layerGroups.quakes).bindPopup(
        '<strong>M ' + A.esc(mag != null ? String(mag) : '—') + '</strong> ' + A.esc(A.pick(q, ['region', 'place']) || '') + '<br>' +
        A.esc(t('quakes.depth')) + ': ' + (depth != null ? A.esc(String(depth)) + ' km' : '—') + '<br>' +
        A.esc(tm ? A.fmtDateTime(tm) : '—') + '<br>' + A.kindTag('detection'));
    });
  }

  function stopQuakeAnim() {
    if (quakeAnim) { clearInterval(quakeAnim); quakeAnim = null; }
  }

  function startQuakeAnim(host) {
    if (!quakeList.length) return;
    const speed = +(host.querySelector('#quake-speed') || {}).value || 1;
    const times = quakeList.map(q => new Date(A.pick(q, ['time', 'occurred_at', 'origin_time']) || 0).getTime())
      .filter(x => x > 0).sort((a, b) => a - b);
    if (!times.length) return;
    const start = times[0], end = Date.now();
    const dayMs = 86400000;
    let cursor = start;
    const label = host.querySelector('#quake-day-label');
    const btn = host.querySelector('#quake-play');
    drawQuakes(cursor);
    quakeAnim = setInterval(() => {
      cursor += dayMs * speed;
      if (cursor >= end) { stopQuakeAnim(); drawQuakes(Infinity); if (label) label.textContent = ''; if (btn) btn.textContent = t('mapx.play'); return; }
      drawQuakes(cursor);
      if (label) label.textContent = new Date(cursor).toLocaleDateString();
    }, 400);
    if (btn) btn.textContent = t('mapx.pause');
  }

  function toggleQuakeAnim(host) {
    if (quakeAnim) { stopQuakeAnim(); drawQuakes(Infinity); host.querySelector('#quake-play').textContent = t('mapx.play'); const l = host.querySelector('#quake-day-label'); if (l) l.textContent = ''; }
    else startQuakeAnim(host);
  }

  /* ---------- tsunami zones: official messages only ---------- */
  async function loadTsunami(host) {
    const res = await A.api.request('/tsunami');
    if (!res.data || !map || !layerGroups.tsunami) return;
    const msgs = A.pick(res.data, ['messages', 'zones', 'events', 'data']) || [];
    const list = Array.isArray(msgs) ? msgs : [];
    layerGroups.tsunami.clearLayers();
    list.slice(0, 50).forEach(m => {
      const poly = A.pick(m, ['polygon', 'coordinates', 'geometry']);
      const cat = A.pick(m, ['raw_classification', 'category', 'classification']) || 'INFORMATION';
      const src = A.pick(m, ['source', 'agency']) || '—';
      const issued = A.pick(m, ['issued_at', 'issue_time', 'time']);
      const updated = A.pick(m, ['updated_at', 'last_update']);
      const popup = '<strong>' + A.esc(String(cat)) + '</strong><br>' +
        A.esc(t('common.source')) + ': ' + A.esc(String(src)) + '<br>' +
        A.esc(t('tsunami.issue_time')) + ': ' + A.esc(issued ? A.fmtDateTime(issued) : '—') + '<br>' +
        A.esc(t('tsunami.last_update')) + ': ' + A.esc(updated ? A.fmtDateTime(updated) : '—') + '<br>' + A.kindTag('warning');
      let layer = null;
      try {
        if (poly && poly.type === 'Polygon' && Array.isArray(poly.coordinates)) {
          const latlngs = poly.coordinates[0].map(c => [c[1], c[0]]);
          layer = L.polygon(latlngs, { color: '#b91c1c', weight: 2, fillOpacity: 0.25 });
        } else {
          const lat = A.pick(m, ['lat', 'latitude']), lon = A.pick(m, ['lon', 'longitude']);
          if (lat != null && lon != null) layer = L.circle([lat, lon], { radius: 150000, color: '#b91c1c', weight: 2, fillOpacity: 0.2 });
        }
      } catch (e) { layer = null; }
      if (layer) layer.addTo(layerGroups.tsunami).bindPopup(popup);
    });
    const prov = res.data.provenance || {};
    setMeta('tsunami', 'official classification', (prov.sources || []).join(', ') || 'nws/usgs', prov.updated_at);
  }
})();
