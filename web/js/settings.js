/* AETHER Settings — language, units, notifications, safety mode,
   saved places, about + data-source attribution (SOURCE_REGISTRY). */
(function () {
  'use strict';
  const A = window.AETHER;
  const t = (k, v) => A.i18n.t(k, v);

  const SOURCES = [
    { key: 'settings.src.openmeteo', terms: 'https://open-meteo.com/en/terms' },
    { key: 'settings.src.usgs', terms: 'https://www.usgs.gov/information-policies-and-instructions/copyrights-and-credits' },
    { key: 'settings.src.nws', terms: 'https://www.weather.gov/disclaimer' }
  ];

  function render(host) {
    const s = A.settings;
    let html = '<div class="card"><h2>' + A.esc(t('settings.title')) + '</h2>';

    html += '<label class="field"><span>' + A.esc(t('settings.language')) + '</span><select id="set-lang" aria-label="' + A.esc(t('settings.language')) + '">' +
      [['my', 'မြန်မာ'], ['en', 'English'], ['ja', '日本語']].map(o =>
        '<option value="' + o[0] + '"' + (s.get('lang') === o[0] ? ' selected' : '') + '>' + o[1] + '</option>').join('') + '</select></label>';

    html += '<label class="field"><span>' + A.esc(t('settings.units')) + '</span><select id="set-units">' +
      '<option value="metric"' + (s.get('units') === 'metric' ? ' selected' : '') + '>' + A.esc(t('settings.units.metric')) + '</option>' +
      '<option value="imperial"' + (s.get('units') === 'imperial' ? ' selected' : '') + '>' + A.esc(t('settings.units.imperial')) + '</option></select></label>';

    html += '<h3>🔔 ' + A.esc(t('settings.notif')) + '</h3>' +
      '<label class="switch"><input type="checkbox" id="set-notif" ' + (s.get('notifEnabled') ? 'checked' : '') + '>' +
      '<span class="switch-track" aria-hidden="true"></span><span class="switch-label">' + A.esc(t('settings.notif.enable')) + '</span></label><br><br>' +
      '<label class="switch"><input type="checkbox" id="set-critonly" ' + (s.get('criticalOnly') ? 'checked' : '') + '>' +
      '<span class="switch-track" aria-hidden="true"></span><span class="switch-label">' + A.esc(t('settings.notif.critical_only')) + '</span></label>';

    html += '<h3>🛡️ ' + A.esc(t('settings.safety')) + '</h3>' +
      '<label class="switch"><input type="checkbox" id="set-safety" ' + (s.get('safetyMode') ? 'checked' : '') + '>' +
      '<span class="switch-track" aria-hidden="true"></span><span class="switch-label">' + A.esc(t('header.safety_mode')) + '</span></label>' +
      '<p class="map-note">' + A.esc(t('settings.safety.desc')) + '</p>';

    /* === AETHER v2-C: expert mode toggle (workstream C, §100) === */
    html += '<h3>🔬 ' + A.esc(t('xp.title')) + '</h3>' +
      '<label class="switch"><input type="checkbox" id="set-expert" ' + (s.get('expertMode') ? 'checked' : '') + '>' +
      '<span class="switch-track" aria-hidden="true"></span><span class="switch-label">' + A.esc(t('xp.title')) + '</span></label>' +
      '<p class="map-note">' + A.esc(t('xp.desc')) + '</p>';

    html += '</div>';

    /* === AETHER v2-C: silent hours (workstream C, §16) === */
    html += '<div class="card"><h2>🔇 ' + A.esc(t('sf.silent_hours')) + '</h2>' +
      '<div class="grid2">' +
      '<label class="field"><span>' + A.esc(t('sf.silent_from')) + '</span><input type="text" id="sf-from" inputmode="numeric" placeholder="22:00"></label>' +
      '<label class="field"><span>' + A.esc(t('sf.silent_to')) + '</span><input type="text" id="sf-to" inputmode="numeric" placeholder="06:00"></label></div>' +
      '<label class="field"><span>' + A.esc(t('sf.silent_tz')) + '</span><input type="text" id="sf-tz" placeholder="Asia/Yangon"></label>' +
      '<div class="field"><span>' + A.esc(t('sf.silent_days')) + '</span><div id="sf-days">' +
      ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'].map((d, i) =>
        '<label class="switch" style="display:inline-flex;margin-right:8px"><input type="checkbox" value="' + i + '" checked>' +
        '<span class="switch-track" aria-hidden="true"></span><span class="switch-label">' + d + '</span></label>').join('') +
      '</div></div>' +
      '<label class="switch"><input type="checkbox" id="sf-override" checked>' +
      '<span class="switch-track" aria-hidden="true"></span><span class="switch-label">' + A.esc(t('sf.silent_override')) + '</span></label><br><br>' +
      '<button type="button" class="btn btn-primary" id="sf-save">' + A.esc(t('sf.save')) + '</button> ' +
      '<span class="map-note" id="sf-msg"></span></div>';

    /* === AETHER v2-C: personalized thresholds (workstream C, §48) === */
    html += '<div class="card"><h2>🎚️ ' + A.esc(t('sf.thresholds')) + '</h2>' +
      '<label class="field"><span>' + A.esc(t('settings.places.name')) + '</span><select id="th-loc"><option value="">…</option></select></label>' +
      '<div class="grid2">' +
      '<label class="field"><span>' + A.esc(t('sf.th_min_severity')) + '</span><select id="th-minsev">' +
      ['INFORMATION', 'LOW', 'MODERATE', 'HIGH', 'SEVERE', 'LIFE_SAFETY'].map(v =>
        '<option value="' + v + '"' + (v === 'LOW' ? ' selected' : '') + '>' + v + '</option>').join('') + '</select></label>' +
      '<label class="field"><span>' + A.esc(t('sf.th_radius')) + '</span><input type="number" id="th-radius" value="100" min="1" step="1"></label>' +
      '<label class="field"><span>' + A.esc(t('sf.th_quake_mag')) + '</span><input type="number" id="th-qmag" step="0.1" min="0" placeholder="4.5"></label>' +
      '<label class="field"><span>' + A.esc(t('sf.th_rain')) + '</span><input type="number" id="th-rain" step="1" min="0" placeholder="20"></label>' +
      '<label class="field"><span>' + A.esc(t('sf.th_wind')) + '</span><input type="number" id="th-wind" step="1" min="0" placeholder="60"></label>' +
      '<label class="field"><span>' + A.esc(t('sf.th_temp_high')) + '</span><input type="number" id="th-thi" step="0.5" placeholder="40"></label>' +
      '<label class="field"><span>' + A.esc(t('sf.th_temp_low')) + '</span><input type="number" id="th-tlo" step="0.5" placeholder="0"></label>' +
      '<label class="field"><span>' + A.esc(t('sf.th_aqi')) + '</span><input type="number" id="th-aqi" step="1" min="0" placeholder="150"></label></div>' +
      '<div class="field"><span>' + A.esc(t('sf.th_hazards')) + '</span><div id="th-haz">' +
      ['EARTHQUAKE', 'TSUNAMI', 'WEATHER', 'AIR_QUALITY'].map(h =>
        '<label class="switch" style="display:inline-flex;margin-right:8px"><input type="checkbox" value="' + h + '" checked>' +
        '<span class="switch-track" aria-hidden="true"></span><span class="switch-label">' + h + '</span></label>').join('') +
      '</div></div>' +
      '<button type="button" class="btn btn-primary" id="th-save">' + A.esc(t('sf.save')) + '</button> ' +
      '<span class="map-note" id="th-msg"></span></div>';

    /* === AETHER v2-C: travel mode (workstream C, §99) === */
    html += '<div class="card"><h2>✈️ ' + A.esc(t('trv.title')) + '</h2>' +
      '<p class="map-note">' + A.esc(t('trv.desc')) + '</p>' +
      '<p class="map-note" id="trv-status"></p>' +
      '<div class="grid2">' +
      '<label class="field"><span>' + A.esc(t('trv.destination')) + '</span><input type="text" id="trv-name" placeholder="Bangkok"></label>' +
      '<label class="field"><span>' + A.esc(t('trv.lat')) + '</span><input type="number" id="trv-lat" step="0.01" min="-90" max="90"></label>' +
      '<label class="field"><span>' + A.esc(t('trv.lon')) + '</span><input type="number" id="trv-lon" step="0.01" min="-180" max="180"></label>' +
      '<label class="field"><span>' + A.esc(t('trv.start')) + '</span><input type="date" id="trv-start"></label>' +
      '<label class="field"><span>' + A.esc(t('trv.end')) + '</span><input type="date" id="trv-end"></label></div>' +
      '<button type="button" class="btn btn-primary" id="trv-save">' + A.esc(t('trv.enable')) + '</button> ' +
      '<button type="button" class="btn" id="trv-clear">' + A.esc(t('trv.disable')) + '</button> ' +
      '<span class="map-note" id="trv-msg"></span></div>';

    html += '<div class="card"><h2>📍 ' + A.esc(t('settings.places')) + '</h2><ul class="timeline" id="places-list">' +
      A.store.all.map(l =>
        '<li><div class="t-body"><strong>' + A.esc(l.name) + '</strong> <span class="map-note">' + l.lat + ', ' + l.lon + '</span>' +
        (A.store.activeId === l.id ? ' ●' : '') +
        ' <button type="button" class="btn btn-small" data-use="' + A.esc(l.id) + '">' + A.esc(t('common.view')) + '</button> ' +
        (A.store.all.length > 1 ? '<button type="button" class="btn btn-small" data-del="' + A.esc(l.id) + '">' + A.esc(t('settings.places.remove')) + '</button>' : '') +
        '</div></li>').join('') + '</ul>' +
      '<h3>' + A.esc(t('settings.places.add')) + '</h3>' +
      '<div class="grid3"><label class="field"><span>' + A.esc(t('settings.places.name')) + '</span><input type="text" id="pl-name"></label>' +
      '<label class="field"><span>' + A.esc(t('settings.places.lat')) + '</span><input type="number" id="pl-lat" step="0.01" min="-90" max="90"></label>' +
      '<label class="field"><span>' + A.esc(t('settings.places.lon')) + '</span><input type="number" id="pl-lon" step="0.01" min="-180" max="180"></label></div>' +
      '<button type="button" class="btn btn-primary" id="pl-add">' + A.esc(t('settings.places.add')) + '</button></div>';

    html += '<div class="card"><h2>' + A.esc(t('settings.cache')) + '</h2>' +
      '<button type="button" class="btn" id="cache-clear">' + A.esc(t('settings.cache.clear')) + '</button> ' +
      '<span class="map-note" id="cache-msg"></span></div>';

    html += '<div class="card"><h2>' + A.esc(t('settings.about')) + '</h2>' +
      '<p><strong>AETHER</strong> · ' + A.esc(t('app.tagline')) + ' · ' + A.esc(t('app.version')) + '</p>' +
      '<h3>' + A.esc(t('settings.sources')) + '</h3>' +
      '<p class="map-note">' + A.esc(t('settings.sources.note')) + '</p><ul>' +
      SOURCES.map(x => '<li><strong>' + A.esc(t(x.key)) + '</strong><br><span class="map-note">' +
        A.esc(t('settings.src.terms')) + ': <a href="' + x.terms + '" target="_blank" rel="noopener">' + A.esc(x.terms) + '</a></span></li>').join('') +
      '</ul><p class="map-note" id="health-line"></p></div>';

    host.innerHTML = html;
    A.i18n.applyStatic(host);

    host.querySelector('#set-lang').addEventListener('change', async e => {
      s.set('lang', e.target.value);
      await A.i18n.setLang(e.target.value);
      A.events.emit('language');
    });
    host.querySelector('#set-units').addEventListener('change', e => { s.set('units', e.target.value); A.events.emit('units'); });
    host.querySelector('#set-notif').addEventListener('change', e => s.set('notifEnabled', e.target.checked));
    host.querySelector('#set-critonly').addEventListener('change', e => s.set('criticalOnly', e.target.checked));
    host.querySelector('#set-safety').addEventListener('change', e => { s.set('safetyMode', e.target.checked); A.safety.apply(); });
    host.querySelectorAll('[data-use]').forEach(b => b.addEventListener('click', () => { A.store.setActive(b.dataset.use); A.show('home'); }));
    host.querySelectorAll('[data-del]').forEach(b => b.addEventListener('click', () => { A.store.remove(b.dataset.del); render(host); }));
    host.querySelector('#pl-add').addEventListener('click', () => {
      const name = host.querySelector('#pl-name').value.trim();
      const lat = parseFloat(host.querySelector('#pl-lat').value), lon = parseFloat(host.querySelector('#pl-lon').value);
      if (!name || isNaN(lat) || isNaN(lon) || lat < -90 || lat > 90 || lon < -180 || lon > 180) return;
      const id = 'p' + Date.now().toString(36);
      A.store.add({ id, name, lat: Math.round(lat * 100) / 100, lon: Math.round(lon * 100) / 100 });
      render(host);
    });
    host.querySelector('#cache-clear').addEventListener('click', () => {
      A.cache.clear();
      host.querySelector('#cache-msg').textContent = t('settings.cache.cleared');
    });

    /* === AETHER v2-C listeners: expert mode, silent hours, thresholds, travel (workstream C) === */
    (function v2c() {
      const base = ((A.config && A.config.apiBase) || '') + '/v1';
      const num = id => { const v = parseFloat(host.querySelector(id).value); return isNaN(v) ? null : v; };
      async function put(path, body) {
        const r = await fetch(base + path, { method: 'PUT', headers: { 'Content-Type': 'application/json', Accept: 'application/json' }, body: JSON.stringify(body) });
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      }
      host.querySelector('#set-expert').addEventListener('change', e => s.set('expertMode', e.target.checked));

      // silent hours: load current, save new
      A.api.request('/notify/preferences?user_id=local').then(res => {
        const p = (res.data && res.data.preferences && res.data.preferences[0]) || {};
        if (p.silent_start) host.querySelector('#sf-from').value = p.silent_start;
        if (p.silent_end) host.querySelector('#sf-to').value = p.silent_end;
        if (p.silent_tz) host.querySelector('#sf-tz').value = p.silent_tz;
        host.querySelector('#sf-override').checked = p.silent_override_life_safety !== 0;
        if (p.silent_days) {
          try {
            const days = JSON.parse(p.silent_days);
            host.querySelectorAll('#sf-days input').forEach(c => { c.checked = days.indexOf(parseInt(c.value, 10)) >= 0; });
          } catch (e) { /* keep defaults */ }
        }
      }).catch(() => { /* offline: keep defaults */ });
      host.querySelector('#sf-save').addEventListener('click', async () => {
        const msg = host.querySelector('#sf-msg');
        try {
          const days = Array.from(host.querySelectorAll('#sf-days input:checked')).map(c => parseInt(c.value, 10));
          await put('/notify/preferences', {
            user_id: 'local',
            silent_start: host.querySelector('#sf-from').value.trim() || null,
            silent_end: host.querySelector('#sf-to').value.trim() || null,
            silent_days: days, silent_tz: host.querySelector('#sf-tz').value.trim() || null,
            silent_override_life_safety: host.querySelector('#sf-override').checked ? 1 : 0
          });
          // mirror to local quiet-hours display so the alerts tab agrees
          const f = host.querySelector('#sf-from').value.trim(), tt2 = host.querySelector('#sf-to').value.trim();
          if (f) s.set('quietFrom', f); if (tt2) s.set('quietTo', tt2);
          msg.textContent = t('sf.saved');
        } catch (e) { msg.textContent = t('common.unavailable.title'); }
      });

      // thresholds: populate locations, save
      A.api.request('/locations').then(res => {
        const locs = (res.data && res.data.locations) || [];
        host.querySelector('#th-loc').innerHTML = locs.map(l =>
          '<option value="' + l.id + '">' + A.esc(l.name) + '</option>').join('');
      }).catch(() => { /* offline */ });
      host.querySelector('#th-save').addEventListener('click', async () => {
        const msg = host.querySelector('#th-msg');
        try {
          const locId = parseInt(host.querySelector('#th-loc').value, 10);
          if (isNaN(locId)) { msg.textContent = t('common.unavailable.title'); return; }
          const haz = Array.from(host.querySelectorAll('#th-haz input:checked')).map(c => c.value);
          await put('/notify/preferences', {
            user_id: 'local', location_id: locId, enabled: 1,
            min_severity: host.querySelector('#th-minsev').value,
            radius_km: num('#th-radius'), quake_mag: num('#th-qmag'),
            rain_mm_h: num('#th-rain'), wind_kmh: num('#th-wind'),
            temp_high_c: num('#th-thi'), temp_low_c: num('#th-tlo'),
            aqi: num('#th-aqi'), hazard_types: haz
          });
          msg.textContent = t('sf.saved');
        } catch (e) { msg.textContent = t('common.unavailable.title'); }
      });

      // travel mode: status, enable, disable
      function trvStatus() {
        A.api.request('/notify/travel?user_id=local').then(res => {
          const tv = res.data && res.data.travel;
          const elx = host.querySelector('#trv-status');
          if (tv && tv.destination_name) {
            elx.textContent = t('trv.active') + ': ' + tv.destination_name +
              ' (' + tv.start_date + ' → ' + tv.end_date + ')';
          } else { elx.textContent = ''; }
        }).catch(() => { /* offline */ });
      }
      trvStatus();
      host.querySelector('#trv-save').addEventListener('click', async () => {
        const msg = host.querySelector('#trv-msg');
        try {
          const lat = num('#trv-lat'), lon = num('#trv-lon');
          const name = host.querySelector('#trv-name').value.trim();
          const sd = host.querySelector('#trv-start').value, ed = host.querySelector('#trv-end').value;
          if (!name || lat === null || lon === null || !sd || !ed) { msg.textContent = t('common.unavailable.title'); return; }
          await put('/notify/travel', { user_id: 'local', destination_name: name, latitude: lat, longitude: lon, start_date: sd, end_date: ed });
          msg.textContent = t('sf.saved'); trvStatus();
        } catch (e) { msg.textContent = t('common.unavailable.title'); }
      });
      host.querySelector('#trv-clear').addEventListener('click', async () => {
        const msg = host.querySelector('#trv-msg');
        try {
          await fetch(base + '/notify/travel?user_id=local', { method: 'DELETE' });
          msg.textContent = t('sf.saved'); trvStatus();
        } catch (e) { msg.textContent = t('common.unavailable.title'); }
      });
    })();

    // backend health → source status line (honest: only what /v1/health reports)
    A.api.request('/health').then(res => {
      const elx = host.querySelector('#health-line');
      if (!elx) return;
      if (res.data) {
        const p = A.provOf(res.data);
        elx.textContent = t('settings.src.status') + ': OK · ' +
          (p.updated_at ? t('common.updated_ago', { min: A.minutesSince(p.updated_at) }) : '');
      } else {
        elx.textContent = t('settings.src.status') + ': ' + t('common.unavailable.title');
      }
    });
  }

  A.views.settings = { render };
})();
