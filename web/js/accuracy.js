/* AETHER Accuracy — per-task metrics (MAE/RMSE/Brier/CRPS/calibration)
   from /v1/accuracy. Forecast-vs-observed mini charts on raw <canvas>
   (no chart library). Explicit "probability ≠ accuracy" note.
   v2: baselines (persistence/climatology), forecast-vs-observed by
   location×variable×horizon×model, model comparison, Trust Index,
   Time Machine replay, source trace, probability explainer. */
(function () {
  'use strict';
  const A = window.AETHER;
  const t = (k, v) => A.i18n.t(k, v);

  /* ============ v1 (kept working) ============ */

  function metricsTable(task) {
    const name = A.pick(task, ['task', 'name']) || '—';
    const metrics = A.pick(task, ['metrics']) || {};
    const rows = [
      ['MAE', metrics.mae], ['RMSE', metrics.rmse], ['Brier', metrics.brier],
      ['CRPS', metrics.crps], [t('accuracy.cal'), metrics.calibration_error]
    ].filter(r => r[1] != null);
    if (!rows.length) return '';
    const win = A.pick(task, ['window', 'period']);
    const n = A.pick(task, ['n', 'samples']);
    return '<h3>' + A.esc(String(name)) + '</h3>' +
      '<table class="metrics"><thead><tr><th>' + A.esc(t('accuracy.metric')) + '</th><th>' +
      A.esc(t('accuracy.value')) + '</th></tr></thead><tbody>' +
      rows.map(r => '<tr><td>' + A.esc(r[0]) + '</td><td>' + A.esc(fmtNum(r[1])) + '</td></tr>').join('') +
      '</tbody></table>' +
      '<p class="map-note">' + A.esc(t('accuracy.window')) + ': ' + A.esc(win != null ? String(win) : '—') +
      ' · ' + A.esc(t('accuracy.n')) + ': ' + A.esc(n != null ? String(n) : '—') + '</p>';
  }

  function fmtNum(v) {
    if (typeof v === 'number') return (Math.round(v * 1000) / 1000).toString();
    return String(v);
  }

  function drawChart(canvas, pairs) {
    const ctx = canvas.getContext('2d');
    const W = canvas.width = canvas.clientWidth * 2 || 600;
    const H = canvas.height = 360;
    ctx.clearRect(0, 0, W, H);
    const fs = pairs.map(p => +p[0]).filter(v => !isNaN(v));
    const os = pairs.map(p => +p[1]).filter(v => !isNaN(v));
    if (!fs.length || !os.length) return;
    const all = fs.concat(os);
    const lo = Math.min.apply(null, all), hi = Math.max.apply(null, all);
    const span = (hi - lo) || 1;
    const X = i => 30 + (W - 50) * (i / Math.max(1, pairs.length - 1));
    const Y = v => H - 30 - (H - 60) * ((v - lo) / span);
    // axes
    ctx.strokeStyle = '#888'; ctx.lineWidth = 2;
    ctx.beginPath(); ctx.moveTo(30, 10); ctx.lineTo(30, H - 30); ctx.lineTo(W - 20, H - 30); ctx.stroke();
    // forecast line
    ctx.strokeStyle = '#0b5fa5'; ctx.lineWidth = 3; ctx.beginPath();
    fs.forEach((v, i) => { const x = X(i), y = Y(v); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
    ctx.stroke();
    // observed line (dashed + markers: shape differs, not color alone)
    ctx.strokeStyle = '#b91c1c'; ctx.setLineDash([10, 6]); ctx.beginPath();
    os.forEach((v, i) => { const x = X(i), y = Y(v); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
    ctx.stroke(); ctx.setLineDash([]);
    ctx.fillStyle = '#b91c1c';
    os.forEach((v, i) => { ctx.beginPath(); ctx.arc(X(i), Y(v), 6, 0, 7); ctx.fill(); });
    ctx.fillStyle = '#888'; ctx.font = '22px sans-serif';
    ctx.fillText('— ' + t('accuracy.legend.forecast'), 40, 34);
    ctx.fillText('● - - ' + t('accuracy.legend.observed'), 40, 62);
  }

  /* ============ v2 helpers ============ */

  let _locId = null;
  async function resolveLocId() {
    if (_locId) return _locId;
    try {
      const loc = A.store.active;
      const res = await A.api.request('/locations');
      const list = (res.data && res.data.locations) || [];
      let best = null, bestD = 1e9;
      list.forEach(l => {
        const d = Math.abs((l.latitude || 0) - loc.lat) + Math.abs((l.longitude || 0) - loc.lon);
        if (d < bestD) { bestD = d; best = l; }
      });
      _locId = best ? best.id : 1;
    } catch (e) { _locId = 1; }
    return _locId;
  }

  function metricRows(m) {
    const rows = [
      ['MAE', m.mae], ['RMSE', m.rmse], ['Bias', m.bias],
      ['Brier', m.brier], ['CRPS', m.crps],
      [t('accuracy.cal'), m.calibration_error]
    ].filter(r => r[1] != null);
    return rows;
  }

  function metricsTable2(m, note) {
    const rows = metricRows(m);
    if (!rows.length) return '';
    let html = '<table class="metrics"><thead><tr><th>' + A.esc(t('accuracy.metric')) +
      '</th><th>' + A.esc(t('accuracy.value')) + '</th></tr></thead><tbody>' +
      rows.map(r => '<tr><td>' + A.esc(r[0]) + '</td><td>' + A.esc(fmtNum(r[1])) + '</td></tr>').join('') +
      '</tbody></table>';
    html += '<p class="map-note">' + A.esc(t('acc2.samples')) + ': ' + A.esc(String(m.sample_count != null ? m.sample_count : '—'));
    if (m.crps_note) html += ' · ' + A.esc(m.crps_note);
    html += '</p>';
    if (note) html += '<p class="map-note">' + A.esc(note) + '</p>';
    return html;
  }

  function probExplainerBtn() {
    return '<button type="button" class="btn btn-small" data-prob-exp>' + A.esc(t('tr.prob_explainer')) + '</button>';
  }

  function bindProbExplainers(host) {
    host.querySelectorAll('[data-prob-exp]').forEach(btn => {
      btn.addEventListener('click', () => {
        const ov = document.createElement('div');
        ov.className = 'modal-overlay';
        ov.innerHTML = '<div class="modal" role="dialog" aria-modal="true" aria-label="' +
          A.esc(t('tr.prob_explainer')) + '"><h3>' + A.esc(t('tr.prob_explainer')) + '</h3>' +
          '<p>' + A.esc(t('tr.prob_explainer.text')) + '</p>' +
          '<button type="button" class="btn btn-primary" data-close>' + A.esc(t('common.close')) + '</button></div>';
        ov.querySelector('[data-close]').addEventListener('click', () => ov.remove());
        ov.addEventListener('click', e => { if (e.target === ov) ov.remove(); });
        document.body.appendChild(ov);
      });
    });
  }

  /* ---- v2 section: forecast vs observed (location×variable×horizon×model) ---- */

  const VARIABLES = ['temperature', 'feels_like', 'humidity', 'pressure', 'wind_speed',
    'precipitation', 'precip_probability', 'cloud_cover'];
  const HORIZONS = [24, 48, 72];
  const MODELS = ['gfs', 'icon_seamless', 'ecmwf_ifs', 'jma_seamless'];
  const WINDOWS = [24, 48, 72, 168];

  function sel(id, opts, cur, label) {
    return '<label>' + A.esc(label) + ' <select id="' + id + '">' +
      opts.map(o => '<option value="' + A.esc(String(o)) + '"' +
        (String(o) === String(cur) ? ' selected' : '') + '>' + A.esc(String(o)) + '</option>').join('') +
      '</select></label>';
  }

  async function loadCompare(host) {
    const box = host.querySelector('#acc2-compare-body');
    const locId = await resolveLocId();
    const v = host.querySelector('#acc2-var').value;
    const h = host.querySelector('#acc2-hor').value;
    const m = host.querySelector('#acc2-mod').value;
    const w = host.querySelector('#acc2-win').value;
    box.innerHTML = '<div class="skeleton" style="height:120px"></div>';
    const res = await A.api.request('/accuracy/compare?location_id=' + locId +
      '&variable=' + encodeURIComponent(v) + '&horizon=' + h +
      '&model=' + encodeURIComponent(m) + '&window=' + w);
    const d = res.data;
    if (!d || !d.series || !d.series.length) {
      box.innerHTML = '<p class="map-note">' + A.esc(t('acc2.no_data')) + '</p>';
      return;
    }
    const chronological = d.series.slice().reverse();
    const pairs = chronological.map(s => [s.forecast, s.observed]);
    let html = metricsTable2(d.metrics, d.metrics.probability_note) + probExplainerBtn();
    html += '<h4>' + A.esc(t('accuracy.chart')) + '</h4>' +
      '<canvas class="chart" id="acc2-chart" data-pairs=\'' +
      A.esc(JSON.stringify(pairs.slice(-120))) + '\' role="img" aria-label="' + A.esc(t('accuracy.chart')) + '"></canvas>';
    // reliability diagram for probability variables
    const rel = d.metrics.reliability;
    if (rel && rel.bins) {
      html += '<h4>' + A.esc(t('acc2.reliability')) + '</h4>' +
        '<table class="metrics"><thead><tr><th>' + A.esc(t('acc2.bin')) + '</th><th>' +
        A.esc(t('acc2.obs_freq')) + '</th><th>' + A.esc(t('accuracy.n')) + '</th></tr></thead><tbody>' +
        rel.bins.filter(b => b.n).map(b =>
          '<tr><td>' + A.esc(b.bin[0] + '–' + b.bin[1]) + '</td><td>' + A.esc(fmtNum(b.observed_freq)) +
          '</td><td>' + A.esc(String(b.n)) + '</td></tr>').join('') + '</tbody></table>';
    }
    // source trace for the newest forecast row
    const newest = d.series[0];
    html += '<details class="trace"><summary>' + A.esc(t('acc2.trace')) + '</summary>' +
      '<div id="acc2-trace-body"><div class="skeleton" style="height:60px"></div></div></details>';
    box.innerHTML = html;
    bindProbExplainers(box);
    const cv = box.querySelector('#acc2-chart');
    try { drawChart(cv, JSON.parse(cv.dataset.pairs || '[]')); } catch (e) { /* blank */ }
    const tr = await A.api.request('/trust/lineage?kind=weather-forecast&location_id=' + locId +
      '&model=' + encodeURIComponent(m) + '&valid_time=' + encodeURIComponent(newest.valid_time));
    const tb = box.querySelector('#acc2-trace-body');
    if (tr.data && tr.data.chain) {
      tb.innerHTML = '<ol class="lineage">' + tr.data.chain.map(n =>
        '<li><strong>' + A.esc(n.title) + '</strong>' +
        (n.missing ? ' <em>(' + A.esc(t('adm.no_data')) + ')</em>' : '') +
        '<br><span class="map-note">' + A.esc(n.detail) + '</span></li>').join('') + '</ol>';
    } else {
      tb.innerHTML = '<p class="map-note">' + A.esc(t('tr.lineage.empty')) + '</p>';
    }
  }

  /* ---- v2 section: baselines ---- */

  async function loadBaselines(host) {
    const box = host.querySelector('#acc2-baselines-body');
    const locId = await resolveLocId();
    const v = host.querySelector('#acc2b-var').value;
    const h = host.querySelector('#acc2b-hor').value;
    box.innerHTML = '<div class="skeleton" style="height:120px"></div>';
    const res = await A.api.request('/accuracy/baselines?location_id=' + locId +
      '&variable=' + encodeURIComponent(v) + '&horizon=' + h);
    const d = res.data;
    if (!d || !d.baselines || !Object.keys(d.baselines).length) {
      box.innerHTML = '<p class="map-note">' + A.esc(t('acc2.no_data')) + '</p>';
      return;
    }
    let html = '<p class="map-note">' + A.esc(d.method) + ' · ' + A.esc(t('acc2.period')) +
      ': ' + A.esc(d.period_start) + ' → ' + A.esc(d.period_end) + '</p>';
    const defs = {
      persistence: [t('acc2.persistence'), t('acc2.persistence_desc')],
      climatology: [t('acc2.climatology'), t('acc2.climatology_desc')]
    };
    Object.keys(d.baselines).forEach(k => {
      const b = d.baselines[k];
      html += '<h4>' + A.esc((defs[k] || [k])[0]) + '</h4>' +
        '<p class="map-note">' + A.esc((defs[k] || [k, ''])[1]) + '</p>' +
        metricsTable2(b, b.definition);
    });
    box.innerHTML = html;
  }

  /* ---- v2 section: model comparison (never auto-declares a winner) ---- */

  async function loadModels(host) {
    const box = host.querySelector('#acc2-models-body');
    const locId = await resolveLocId();
    box.innerHTML = '<div class="skeleton" style="height:120px"></div>';
    const res = await A.api.request('/accuracy/models?location_id=' + locId +
      '&variable=temperature&horizon=24');
    const d = res.data;
    if (!d || !d.models || !d.models.length) {
      box.innerHTML = '<p class="map-note">' + A.esc(t('acc2.no_data')) + '</p>';
      return;
    }
    let html = '<div class="note" role="note">⚠️ ' + A.esc(t('acc2.no_winner')) + '</div>' +
      '<table class="metrics"><thead><tr><th>' + A.esc(t('acc2.model')) + '</th><th>MAE</th><th>RMSE</th><th>Brier</th><th>' +
      A.esc(t('acc2.samples')) + '</th><th>' + A.esc(t('acc2.period')) + '</th></tr></thead><tbody>' +
      d.models.map(m =>
        '<tr><td>' + A.esc(String(m.model)) + '<br><span class="map-note">' + A.esc(String(m.kind || '')) + '</span></td>' +
        '<td>' + A.esc(fmtNum(m.mae)) + '</td><td>' + A.esc(fmtNum(m.rmse)) + '</td><td>' + A.esc(fmtNum(m.brier)) + '</td>' +
        '<td>' + A.esc(String(m.sample_count != null ? m.sample_count : '—')) + '</td>' +
        '<td class="map-note">' + A.esc(String(m.period_start || '') + ' → ' + String(m.period_end || '')) + '</td></tr>'
      ).join('') + '</tbody></table>' +
      '<p class="map-note">' + A.esc(d.note) + '</p>';
    box.innerHTML = html;
  }

  /* ---- v2 section: Trust Index ---- */

  async function loadTrust(host) {
    const box = host.querySelector('#acc2-trust-body');
    const locId = await resolveLocId();
    box.innerHTML = '<div class="skeleton" style="height:120px"></div>';
    const res = await A.api.request('/trust/index?location_id=' + locId);
    const d = res.data;
    if (!d || d.trust_index == null) {
      box.innerHTML = '<p class="map-note">' + A.esc(t('acc2.no_data')) + '</p>';
      return;
    }
    const compNames = {
      source_quality: t('tr.quality.title'), freshness: t('tr.quality.freshness'),
      model_agreement: t('home.models'), historical_skill: t('acc2.models'),
      observation_agreement: t('acc2.compare'), uncertainty: t('tr.uncertain')
    };
    let html = '<div class="trust-score"><div class="trust-bar"><div class="trust-fill" style="width:' +
      A.esc(String(Math.max(0, Math.min(100, d.trust_index)))) + '%"></div></div>' +
      '<p><strong>' + A.esc(fmtNum(d.trust_index)) + ' / 100</strong></p>' +
      '<p class="note" role="note">⚠️ ' + A.esc(t('tr.index.disclaimer')) + '</p></div>' +
      '<table class="metrics"><thead><tr><th>' + A.esc(t('accuracy.metric')) + '</th><th>' +
      A.esc(t('accuracy.value')) + '</th><th>' + A.esc(t('smr.note')) + '</th></tr></thead><tbody>' +
      Object.keys(d.components).map(k =>
        '<tr><td>' + A.esc(compNames[k] || k) + '</td><td>' + A.esc(fmtNum(d.components[k])) + '</td>' +
        '<td class="map-note">' + A.esc(String((d.component_notes || {})[k] || '')) + '</td></tr>'
      ).join('') + '</tbody></table>' +
      '<p class="map-note">' + A.esc(d.disclaimer) + '</p>';
    box.innerHTML = html;
  }

  /* ---- v2 section: Time Machine ---- */

  function yesterdayISO() {
    const d = new Date(Date.now() - 86400000);
    return d.toISOString().slice(0, 10);
  }

  function wwwBlock(known, estimated, uncertain) {
    const sec = (key, items) => items.length ?
      '<h5>' + A.esc(t(key)) + '</h5><ul>' + items.map(i => '<li>' + A.esc(i) + '</li>').join('') + '</ul>' : '';
    return '<div class="www">' + sec('tr.known', known) + sec('tr.estimated', estimated) + sec('tr.uncertain', uncertain) + '</div>';
  }

  async function loadReplay(host) {
    const box = host.querySelector('#tm-body');
    const dateEl = host.querySelector('#tm-date');
    const date = dateEl.value;
    if (!date) { box.innerHTML = '<p class="map-note">' + A.esc(t('tm.date')) + '</p>'; return; }
    const locId = await resolveLocId();
    box.innerHTML = '<div class="skeleton" style="height:160px"></div>';
    const res = await A.api.request('/replay?date=' + encodeURIComponent(date) + '&location_id=' + locId);
    const d = res.data;
    if (!d || !d.past) {
      box.innerHTML = '<div class="note" role="note">⚠️ ' + A.esc(t('tm.future_blocked')) + '</div>';
      return;
    }
    const past = d.past;
    // timeline bar: PAST | PRESENT | FORECAST — visually separated
    let html = '<div class="timeline-bar" role="img" aria-label="Past / Present / Forecast">' +
      '<span class="tl-seg tl-past">' + A.esc(t('tm.past')) + ': ' + A.esc(date) + '</span>' +
      '<span class="tl-seg tl-present">' + A.esc(t('tm.present')) + '</span>' +
      '<span class="tl-seg tl-forecast">' + A.esc(t('tm.forecast')) + ' →</span></div>' +
      '<p class="map-note">⚠️ ' + A.esc(t('tm.separation_note')) + '</p>';

    const said = past.forecast_said || [];
    const obs = past.observed || [];
    html += '<div class="replay-cols"><div><h4>📡 ' + A.esc(t('tm.forecast_said')) +
      ' <span class="kind kind-forecast">' + A.esc(t('tm.past')) + '</span></h4>';
    if (said.length) {
      html += '<table class="metrics"><thead><tr><th>' + A.esc(t('tm.date')) +
        '</th><th>' + A.esc(t('acc2.model')) + '</th><th>°C</th><th>%</th></tr></thead><tbody>' +
        said.slice(0, 12).map(s =>
          '<tr><td class="map-note">' + A.esc(String(s.valid_time).slice(11, 16)) + '</td>' +
          '<td>' + A.esc(String(s.model)) + '</td>' +
          '<td>' + A.esc(fmtNum(s.temperature)) + '</td>' +
          '<td>' + A.esc(fmtNum(s.precip_probability)) + '</td></tr>').join('') +
        '</tbody></table>' + (said.length > 12 ? '<p class="map-note">… ' + said.length + '</p>' : '');
    } else {
      html += '<p class="map-note">' + A.esc(t('tm.no_data')) + '</p>';
    }
    html += '</div><div><h4>👁️ ' + A.esc(t('tm.observed')) +
      ' <span class="kind kind-observation">' + A.esc(t('tm.past')) + '</span></h4>';
    if (obs.length) {
      html += '<table class="metrics"><thead><tr><th>' + A.esc(t('tm.date')) +
        '</th><th>°C</th><th>mm</th></tr></thead><tbody>' +
        obs.slice(0, 12).map(o =>
          '<tr><td class="map-note">' + A.esc(String(o.observed_at).slice(11, 16)) + '</td>' +
          '<td>' + A.esc(fmtNum(o.temperature)) + '</td>' +
          '<td>' + A.esc(fmtNum(o.precipitation)) + '</td></tr>').join('') +
        '</tbody></table>' + (obs.length > 12 ? '<p class="map-note">… ' + obs.length + '</p>' : '');
    } else {
      html += '<p class="map-note">' + A.esc(t('tm.no_data')) + '</p>';
    }
    html += '</div></div>';

    // quake sequence
    const quakes = d.quakes || [];
    html += '<h4>🌋 ' + A.esc(t('tm.quakes')) + ' (' + quakes.length + ')</h4>';
    if (quakes.length) {
      html += '<ul>' + quakes.map(q =>
        '<li>M' + A.esc(fmtNum(q.magnitude)) + ' — ' + A.esc(q.region || '') +
        ' <span class="map-note">' + A.esc(q.origin_time) + '</span></li>').join('') + '</ul>';
    } else {
      html += '<p class="map-note">' + A.esc(t('tm.no_data')) + '</p>';
    }

    // tsunami revisions
    const revs = d.tsunami_revisions || {};
    const revIds = Object.keys(revs);
    html += '<h4>🌊 ' + A.esc(t('tm.tsunami')) + ' (' + revIds.length + ')</h4>';
    if (revIds.length) {
      html += revIds.map(id =>
        '<h5>' + A.esc(id) + '</h5><ol>' + revs[id].map(m =>
          '<li><strong>' + A.esc(m.message_type) + '</strong> <span class="map-note">' +
          A.esc(m.issue_time) + '</span></li>').join('') + '</ol>').join('');
    } else {
      html += '<p class="map-note">' + A.esc(t('tm.no_data')) + '</p>';
    }

    // what-we-know block
    const known = [], estimated = [], uncertain = [];
    quakes.forEach(q => known.push('M' + fmtNum(q.magnitude) + ' ' + (q.region || '') + ' — ' + q.origin_time));
    revIds.forEach(id => revs[id].forEach(m => known.push(id + ': ' + m.message_type + ' — ' + m.issue_time)));
    if (obs.length) known.push(obs.length + ' ' + t('tm.observed'));
    said.slice(0, 3).forEach(s => estimated.push(
      String(s.model) + ' +' + s.forecast_hour + 'h: ' + fmtNum(s.temperature) + '°C @ ' + String(s.valid_time).slice(11, 16)));
    if (!said.length) uncertain.push(t('tm.no_data') + ' (' + t('tm.forecast_said') + ')');
    if (!obs.length) uncertain.push(t('tm.no_data') + ' (' + t('tm.observed') + ')');
    if (!quakes.length && !revIds.length) uncertain.push(t('tm.quakes') + ' / ' + t('tm.tsunami') + ': ' + t('tm.no_data'));
    html += wwwBlock(known, estimated, uncertain);

    html += A.updatedLine(d, res);
    box.innerHTML = html;
    A.i18n.applyStatic(box);
  }

  /* ---- v2 section: source health + failover banner ---- */

  async function loadSources(host) {
    const box = host.querySelector('#acc2-sources-body');
    box.innerHTML = '<div class="skeleton" style="height:80px"></div>';
    const res = await A.api.request('/trust/sources');
    const d = res.data;
    if (!d || !d.sources) { box.innerHTML = ''; return; }
    const fo = (d.recent_failovers || [])[0];
    let html = '';
    if (fo) {
      const ageH = fo.occurred_at ? Math.round((Date.now() - new Date(fo.occurred_at).getTime()) / 3600000) : null;
      html += '<div class="note" role="alert">⚠️ <strong>' + A.esc(t('tr.failover.changed')) + '.</strong> ' +
        A.esc(fo.from_source + ' → ' + fo.to_source) +
        (ageH != null ? ' · ' + A.esc(String(ageH)) + 'h' : '') +
        (fo.reason ? ' — ' + A.esc(fo.reason) : '') + '</div>';
    }
    html += '<table class="metrics"><thead><tr><th>' + A.esc(t('adm.sources')) + '</th><th>' +
      A.esc(t('adm.health')) + '</th><th>' + A.esc(t('adm.age')) + '</th></tr></thead><tbody>' +
      d.sources.map(s =>
        '<tr><td>' + A.esc(s.provider || s.source_id) + '<br><span class="map-note">' + A.esc(s.dataset || '') + '</span></td>' +
        '<td><span class="health health-' + A.esc(String(s.health).toLowerCase()) + '">' +
        A.esc(t('tr.status.' + String(s.health).toLowerCase(), null) === 'tr.status.' + String(s.health).toLowerCase() ? s.health : t('tr.status.' + String(s.health).toLowerCase())) +
        '</span><br><span class="map-note">' + A.esc(s.health_reason || '') + '</span></td>' +
        '<td>' + A.esc(s.data_age_min != null ? String(s.data_age_min) : '—') + '</td></tr>'
      ).join('') + '</tbody></table>';
    const stale = d.sources.filter(s => s.health === 'STALE' || s.health === 'FAILED');
    if (stale.length) html += '<p class="note" role="note">⚠️ ' + A.esc(t('tr.stale_warning')) + '</p>';
    box.innerHTML = html;
  }

  /* ============ render ============ */

  async function render(host) {
    host.innerHTML = '<div class="card"><h2>' + A.esc(t('accuracy.title')) + '</h2><div class="skeleton" style="height:160px"></div></div>';
    const res = await A.api.request('/accuracy');
    if (!res.data) {
      host.innerHTML = '<div class="card"><h2>' + A.esc(t('accuracy.title')) + '</h2>' +
        A.emptyState('common.unavailable.title', 'common.unavailable.desc') + '</div>';
      return;
    }
    const data = res.data;
    let tasks = A.pick(data, ['tasks', 'metrics', 'data']) || [];
    if (!Array.isArray(tasks)) tasks = [data];

    let html = '<div class="card"><div style="display:flex;justify-content:space-between;align-items:center;gap:8px">' +
      '<h2>' + A.esc(t('accuracy.title')) + '</h2>' + A.kindTag('observation') + '</div>' +
      '<p class="map-note">' + A.esc(t('accuracy.subtitle')) + '</p>' +
      '<div class="note" role="note">⚠️ <strong>' + A.esc(t('accuracy.prob_note.title')) + '.</strong> ' +
      A.esc(t('accuracy.prob_note.text')) + ' ' + probExplainerBtn() + '</div>';
    tasks.slice(0, 10).forEach(task => { html += metricsTable(task); });

    // forecast vs observed charts (v1)
    const series = A.pick(data, ['series', 'comparisons']) || [];
    if (Array.isArray(series) && series.length) {
      html += '<h3>' + A.esc(t('accuracy.chart')) + '</h3>';
      series.slice(0, 4).forEach((s, i) => {
        const pairs = A.pick(s, ['pairs', 'points']) || [];
        html += '<h4>' + A.esc(String(A.pick(s, ['task', 'name']) || (i + 1))) + '</h4>' +
          '<canvas class="chart" id="acc-chart-' + i + '" data-pairs=\'' +
          A.esc(JSON.stringify(pairs.slice(0, 60))) + '\' role="img" aria-label="' + A.esc(t('accuracy.chart')) + '"></canvas>';
      });
    }
    html += A.updatedLine(data, res) + '</div>';

    // ---- v2 sections ----
    html += '<div class="card"><h2>' + A.esc(t('acc2.trust')) + ' ' + A.kindTag('risk') + '</h2>' +
      '<div id="acc2-trust-body"><div class="skeleton" style="height:120px"></div></div></div>';

    html += '<div class="card"><h2>' + A.esc(t('acc2.compare')) + '</h2>' +
      '<div class="acc2-controls">' +
      sel('acc2-var', VARIABLES, 'temperature', t('acc2.variable')) +
      sel('acc2-hor', HORIZONS, 24, t('acc2.horizon')) +
      sel('acc2-mod', MODELS, 'gfs', t('acc2.model')) +
      sel('acc2-win', WINDOWS, 72, t('acc2.window')) +
      '</div><div id="acc2-compare-body"><div class="skeleton" style="height:120px"></div></div></div>';

    html += '<div class="card"><h2>' + A.esc(t('acc2.baselines')) + '</h2>' +
      '<div class="acc2-controls">' +
      sel('acc2b-var', VARIABLES.filter(v => v !== 'precip_probability'), 'temperature', t('acc2.variable')) +
      sel('acc2b-hor', HORIZONS, 24, t('acc2.horizon')) +
      '</div><div id="acc2-baselines-body"><div class="skeleton" style="height:120px"></div></div></div>';

    html += '<div class="card"><h2>' + A.esc(t('acc2.models')) + '</h2>' +
      '<div id="acc2-models-body"><div class="skeleton" style="height:120px"></div></div></div>';

    html += '<div class="card"><h2>' + A.esc(t('tr.sources.title')) + '</h2>' +
      '<div id="acc2-sources-body"><div class="skeleton" style="height:80px"></div></div></div>';

    html += '<div class="card"><h2>🕰️ ' + A.esc(t('tm.title')) + '</h2>' +
      '<p class="map-note">' + A.esc(t('tm.subtitle')) + '</p>' +
      '<div class="acc2-controls"><label>' + A.esc(t('tm.date')) +
      ' <input type="date" id="tm-date" max="' + yesterdayISO() + '" value="' + yesterdayISO() + '"></label>' +
      '<button type="button" class="btn btn-primary" id="tm-load">' + A.esc(t('tm.load')) + '</button></div>' +
      '<div id="tm-body"></div></div>';

    host.innerHTML = html;
    A.i18n.applyStatic(host);
    host.querySelectorAll('canvas.chart[id^="acc-chart-"]').forEach(cv => {
      try { drawChart(cv, JSON.parse(cv.dataset.pairs || '[]')); } catch (e) { /* leave blank */ }
    });
    bindProbExplainers(host);

    // v2 section loaders
    ['acc2-var', 'acc2-hor', 'acc2-mod', 'acc2-win'].forEach(id => {
      const el = host.querySelector('#' + id);
      if (el) el.addEventListener('change', () => loadCompare(host));
    });
    ['acc2b-var', 'acc2b-hor'].forEach(id => {
      const el = host.querySelector('#' + id);
      if (el) el.addEventListener('change', () => loadBaselines(host));
    });
    const tmBtn = host.querySelector('#tm-load');
    if (tmBtn) tmBtn.addEventListener('click', () => loadReplay(host));
    loadTrust(host); loadCompare(host); loadBaselines(host); loadModels(host); loadSources(host);
    loadReplay(host);
  }

  A.views.accuracy = { render };
})();
