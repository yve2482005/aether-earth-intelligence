/* AETHER Operations dashboard — internal tool (§94).
   Standalone page: no auth (per spec), not linked prominently.
   Every number comes from /v1/admin; missing data renders as "no data". */
(function () {
  'use strict';
  const A = window.AETHER || (window.AETHER = {});
  const t = (k) => (A.i18n ? A.i18n.t(k) : k);

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  async function get(path) {
    try {
      const r = await fetch('/v1' + path, { headers: { Accept: 'application/json' } });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return await r.json();
    } catch (e) {
      return null;
    }
  }

  async function post(path, body) {
    const r = await fetch('/v1' + path, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
    });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }

  function noData() { return '<p class="map-note">' + esc(t('adm.no_data')) + '</p>'; }

  function healthBadge(s) {
    const cls = String(s || 'disabled').toLowerCase();
    const label = t('tr.status.' + cls);
    return '<span class="health health-' + esc(cls) + '">' +
      esc(label === 'tr.status.' + cls ? s : label) + '</span>';
  }

  function setHtml(id, html) {
    const el = document.getElementById(id);
    if (el) {
      el.querySelectorAll('.skeleton').forEach(s => s.remove());
      const body = el.querySelector('.ops-body') || el;
      // keep the h2; replace everything after it
      const h2 = el.querySelector('h2');
      Array.from(el.childNodes).forEach(n => { if (n !== h2) n.remove(); });
      const div = document.createElement('div');
      div.innerHTML = html;
      el.appendChild(div);
      if (A.i18n) A.i18n.applyStatic(el);
    }
  }

  async function renderSources(d) {
    const rows = (d.sources || []).map(s =>
      '<tr><td>' + esc(s.provider || s.source_id) + '<br><span class="map-note">' +
      esc(s.dataset || '') + '</span></td><td>' + healthBadge(s.health) +
      '<br><span class="map-note">' + esc(s.health_reason || '') + '</span></td>' +
      '<td>' + esc(s.last_success || '—') + '<br><span class="map-note">' +
      esc(s.data_age_min != null ? s.data_age_min + ' min' : '—') + '</span></td></tr>').join('');
    let html = rows ?
      '<table class="metrics"><thead><tr><th>' + esc(t('adm.sources')) + '</th><th>' +
      esc(t('adm.health')) + '</th><th>' + esc(t('adm.last_success')) + '</th></tr></thead><tbody>' +
      rows + '</tbody></table>' : noData();
    const fo = (d.failovers || [])[0];
    if (fo) {
      html = '<div class="note" role="alert">⚠️ <strong>' + esc(t('tr.failover.changed')) +
        '.</strong> ' + esc(fo.from_source + ' → ' + fo.to_source) +
        (fo.reason ? ' — ' + esc(fo.reason) : '') +
        ' <span class="map-note">' + esc(fo.occurred_at || '') + '</span></div>' + html;
    }
    setHtml('ops-sources', html);
  }

  async function renderFreshness(d) {
    const rows = (d.freshness || []).map(q =>
      '<tr><td>' + esc(q.dataset) + '</td><td><strong>' + esc(String(q.score)) +
      '</strong> ' + esc(q.label || '') + '</td><td>' +
      esc(q.age_minutes != null ? q.age_minutes + ' min' : '—') + '</td></tr>').join('');
    setHtml('ops-freshness', rows ?
      '<table class="metrics"><thead><tr><th>dataset</th><th>score</th><th>' +
      esc(t('adm.age')) + '</th></tr></thead><tbody>' + rows + '</tbody></table>' : noData());
  }

  async function renderHazards(d) {
    const hz = d.active_hazards || {};
    const q = (hz.quakes_7d_m5plus || []).map(x =>
      '<li>M' + esc(String(x.magnitude)) + ' — ' + esc(x.region || '') +
      ' <span class="map-note">' + esc(x.origin_time) + '</span></li>').join('');
    const ts = (hz.tsunami_active || []).map(x =>
      '<li><strong>' + esc(x.message_type) + '</strong> ' + esc(x.affected_region || '') +
      ' <span class="map-note">' + esc(x.issue_time) + '</span></li>').join('');
    setHtml('ops-hazards',
      '<h4>' + esc(t('smr.quakes')) + '</h4>' + (q ? '<ul>' + q + '</ul>' : noData()) +
      '<h4>' + esc(t('smr.tsunami_active')) + '</h4>' + (ts ? '<ul>' + ts + '</ul>' : noData()));
  }

  async function renderAlerts(d) {
    const aq = d.alert_queue || {};
    const rows = (aq.alerts || []).map(a =>
      '<tr><td>P' + esc(String(a.priority)) + ' ' + esc(a.severity) + '</td><td>' +
      esc(a.title) + '<br><span class="map-note">' + esc(a.type) + ' · ' +
      esc(a.issued_at) + '</span></td></tr>').join('');
    setHtml('ops-alerts',
      '<p class="map-note">' + esc(t('smr.total')) + ': ' + esc(String(aq.count != null ? aq.count : 0)) + '</p>' +
      (rows ? '<table class="metrics"><tbody>' + rows + '</tbody></table>' : noData()));
  }

  async function renderJobs(d) {
    const rows = (d.failed_jobs || []).map(j =>
      '<tr><td>' + esc(j.component) + '</td><td>' + healthBadge(j.status) + '</td><td class="map-note">' +
      esc(j.message || '') + '<br>' + esc(j.checked_at || '') + '</td></tr>').join('');
    setHtml('ops-jobs', rows ?
      '<table class="metrics"><thead><tr><th>component</th><th>' + esc(t('adm.health')) +
      '</th><th>' + esc(t('adm.reason')) + '</th></tr></thead><tbody>' + rows + '</tbody></table>' : noData());
  }

  function incidentCard(inc) {
    const tl = (inc.timeline || []).map(e =>
      '<li><span class="map-note">' + esc(e.at) + '</span> — ' + esc(e.event) + '</li>').join('');
    const id = 'inc-' + inc.id;
    return '<details class="ops inc-' + esc(inc.status) + '"><summary><strong>#' + inc.id +
      '</strong> ' + esc(inc.title) + ' ' + healthBadge(inc.status) +
      ' <span class="map-note">' + esc(t('inc.type.' + inc.type) === 'inc.type.' + inc.type ? inc.type : t('inc.type.' + inc.type)) +
      ' · ' + esc(inc.component) + '</span></summary>' +
      '<p>' + esc(inc.detail || '') + '</p>' +
      '<h4>' + esc(t('inc.timeline')) + '</h4><ul class="tl">' + tl + '</ul>' +
      '<div class="ops-form">' +
      '<label><span data-i18n="adm.inc.note_ph">Note</span><input id="' + id + '-note"></label>' +
      '<div><button type="button" class="btn btn-small" data-inc-note="' + inc.id + '" data-input="' + id + '-note">' +
      esc(t('adm.inc.add_note')) + '</button> ' +
      (inc.status !== 'resolved' ?
        '<button type="button" class="btn btn-small" data-inc-resolve="' + inc.id + '">' +
        esc(t('adm.inc.resolve')) + '</button>' : '') +
      '</div></div></details>';
  }

  async function renderIncidents(d) {
    const open = (d.incidents.open || []).map(incidentCard).join('');
    const recent = (d.incidents.recent || []).map(incidentCard).join('');
    setHtml('ops-incidents',
      '<h4>' + esc(t('inc.status.open')) + ' (' + (d.incidents.open || []).length + ')</h4>' +
      (open || noData()) +
      '<h4>' + esc(t('inc.timeline')) + '</h4>' + (recent || noData()));
    bindIncidentButtons();
  }

  function bindIncidentButtons() {
    document.querySelectorAll('[data-inc-note]').forEach(b => {
      b.onclick = async () => {
        const input = document.getElementById(b.dataset.input);
        const ev = (input.value || '').trim();
        if (!ev) return;
        await post('/admin/incidents/' + b.dataset.incNote + '/timeline', { event: ev });
        load();
      };
    });
    document.querySelectorAll('[data-inc-resolve]').forEach(b => {
      b.onclick = async () => {
        const resolution = prompt(t('adm.inc.resolution'));
        if (!resolution) return;
        await post('/admin/incidents/' + b.dataset.incResolve + '/resolve', { resolution });
        load();
      };
    });
  }

  async function renderModels(d) {
    const m = d.models || {};
    const champ = (m.champions || []).map(c =>
      '<li><strong>' + esc(c.version) + '</strong> <span class="map-note">' + esc(c.task) +
      ' · ' + esc(c.deployed_at || '') + '</span></li>').join('');
    const cand = (m.candidates || []).map(c =>
      '<li>' + esc(c.version) + ' <span class="map-note">' + esc(c.task) + '</span></li>').join('');
    const runs = (m.training_runs || []).map(r =>
      '<tr><td>' + esc(r.run_id) + '</td><td>' + esc(r.task) + '</td><td>' +
      esc(String(r.rows != null ? r.rows : '—')) + '</td><td>' + healthBadge(r.status) +
      '</td><td class="map-note">' + esc(r.started_at || '') + '</td></tr>').join('');
    setHtml('ops-models',
      '<h4>' + esc(t('adm.champions')) + '</h4>' + (champ ? '<ul>' + champ + '</ul>' : noData()) +
      '<h4>' + esc(t('adm.candidates')) + '</h4>' + (cand ? '<ul>' + cand + '</ul>' : noData()) +
      '<h4>' + esc(t('adm.training_runs')) + '</h4>' +
      (runs ? '<table class="metrics"><thead><tr><th>run</th><th>task</th><th>' +
        esc(t('adm.rows')) + '</th><th>' + esc(t('adm.health')) + '</th><th></th></tr></thead><tbody>' +
        runs + '</tbody></table>' : noData()) +
      '<p class="map-note">' + esc(t('adm.rows')) + ': ' +
      esc(String(m.forecast_dataset_rows != null ? m.forecast_dataset_rows : '—')) + '</p>');
  }

  async function renderAccuracy(d) {
    const rows = (d.accuracy_snapshot || []).map(s =>
      '<tr><td>' + esc(s.model) + '<br><span class="map-note">' + esc(s.variable) +
      ' +' + esc(String(s.horizon)) + 'h</span></td><td>' +
      esc(s.mae != null ? Number(s.mae).toFixed(2) : '—') + '</td><td>' +
      esc(s.sample_count != null ? String(s.sample_count) : '—') + '</td><td class="map-note">' +
      esc(s.period_end || '') + '</td></tr>').join('');
    setHtml('ops-accuracy', rows ?
      '<table class="metrics"><thead><tr><th>model</th><th>MAE</th><th>n</th><th></th></tr></thead><tbody>' +
      rows + '</tbody></table>' : noData());
  }

  async function renderDeliveries(d) {
    const nd = d.notification_deliveries || {};
    const stats = nd.stats || {};
    const byStatus = Object.keys(stats.by_status || {}).map(k =>
      '<li>' + esc(k) + ': <strong>' + esc(String(stats.by_status[k])) + '</strong></li>').join('');
    const rows = (nd.recent || []).map(x =>
      '<tr><td>' + esc(x.alert_id) + '</td><td>' + esc(x.channel) + '</td><td>' +
      healthBadge(x.status) + '</td><td class="map-note">' + esc(x.attempted_at) + '</td></tr>').join('');
    setHtml('ops-deliveries',
      '<p class="map-note">' + esc(t('smr.total')) + ': ' + esc(String(stats.total != null ? stats.total : 0)) +
      ' — ' + esc(stats.note || '') + '</p>' +
      (byStatus ? '<ul>' + byStatus + '</ul>' : '') +
      (rows ? '<table class="metrics"><thead><tr><th>alert</th><th>channel</th><th>' +
        esc(t('adm.health')) + '</th><th></th></tr></thead><tbody>' + rows + '</tbody></table>' : noData()));
  }

  async function renderStorage(d) {
    const st = d.storage || {};
    const mb = st.db_bytes != null ? (st.db_bytes / 1048576).toFixed(1) + ' MB' : '—';
    const rows = Object.keys(st.table_counts || {}).map(k =>
      '<tr><td>' + esc(k) + '</td><td>' +
      esc(st.table_counts[k] != null ? String(st.table_counts[k]) : '—') + '</td></tr>').join('');
    setHtml('ops-storage',
      '<p><strong>' + esc(t('adm.db_size')) + ':</strong> ' + esc(mb) + '</p>' +
      (rows ? '<table class="metrics"><thead><tr><th>table</th><th>' + esc(t('adm.rows')) +
        '</th></tr></thead><tbody>' + rows + '</tbody></table>' : ''));
  }

  async function load() {
    const d = await get('/admin/summary');
    if (!d) {
      document.getElementById('main').innerHTML =
        '<div class="card"><p>' + esc(t('common.unavailable.title')) + '</p></div>';
      return;
    }
    document.getElementById('ops-asof').textContent = d.as_of || '—';
    renderSources(d); renderFreshness(d); renderHazards(d); renderAlerts(d);
    renderJobs(d); renderIncidents(d); renderModels(d); renderAccuracy(d);
    renderDeliveries(d); renderStorage(d);
  }

  function fillIncidentTypes() {
    const sel = document.getElementById('inc-type');
    ['source_outage', 'model_failure', 'alert_failure', 'db_failure', 'other'].forEach(ty => {
      const o = document.createElement('option');
      o.value = ty;
      const label = t('inc.type.' + ty);
      o.textContent = label === 'inc.type.' + ty ? ty : label;
      sel.appendChild(o);
    });
  }

  async function init() {
    let lang = 'my';
    try {
      const s = JSON.parse(localStorage.getItem('aether.settings.v1') || '{}');
      if (s.lang) lang = s.lang;
    } catch (e) { /* default */ }
    document.documentElement.lang = lang;
    if (A.i18n) { await A.i18n.setLang(lang); A.i18n.applyStatic(document); }
    fillIncidentTypes();
    document.getElementById('ops-refresh').addEventListener('click', load);
    const form = document.getElementById('inc-form');
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      try {
        await post('/admin/incidents', {
          type: document.getElementById('inc-type').value,
          component: document.getElementById('inc-component').value,
          title: document.getElementById('inc-title').value,
          detail: document.getElementById('inc-detail').value
        });
        form.reset();
        load();
      } catch (err) { alert('Error: ' + err.message); }
    });
    load();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
