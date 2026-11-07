/* AETHER Earthquakes (v2) — list + detail cards.
   Magnitude, depth, distance from user, local+UTC time, region,
   felt reports, tsunami status, source, last update, characterization
   confidence with reasons.
   Event fusion: Original → Revised → Latest from earthquake_updates revision
   history (tap a card for details). Aftershock section is statistical only,
   labeled "statistical aftershock probability, not a prediction" (§8).
   No prediction UI of any kind. */
(function () {
  'use strict';
  const A = window.AETHER;
  const t = (k, v) => A.i18n.t(k, v);
  const detailsCache = {};

  function cardHTML(q) {
    const loc = A.store.active;
    const mag = A.pick(q, ['magnitude', 'mag']);
    const depth = A.pick(q, ['depth_km', 'depth']);
    const region = A.pick(q, ['region', 'place']) || '—';
    const tm = A.pick(q, ['time', 'occurred_at', 'origin_time']);
    const id = A.pick(q, ['canonical_event_id', 'id', 'event_id']);
    const felt = A.pick(q, ['felt_reports', 'felt', 'felt_count']);
    const tsu = A.pick(q, ['tsunami', 'tsunami_status', 'tsunami_flag']);
    const src = A.pick(q, ['source', 'agency']);
    const upd = A.pick(q, ['updated_at', 'update_time', 'last_update']);
    const conf = A.pick(q, ['confidence', 'characterization_confidence']);
    const qlat = A.pick(q, ['lat', 'latitude']), qlon = A.pick(q, ['lon', 'longitude']);
    const dist = A.haversineKm(loc.lat, loc.lon, qlat, qlon);
    const reasons = conf && Array.isArray(conf.reasons) ? conf.reasons : (conf && Array.isArray(conf.why) ? conf.why : []);
    const pct = conf ? (conf.percent != null ? conf.percent : (conf.level === 'high' ? 85 : conf.level === 'medium' ? 65 : 40)) : null;

    return '<article class="card quake" aria-label="' + A.esc(t('nav.quakes') + ' M ' + (mag != null ? mag : '?')) + '">' +
      '<div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap">' +
      '<span class="quake-mag">M ' + A.esc(mag != null ? String(mag) : '—') + '</span>' + A.kindTag('detection') + '</div>' +
      '<dl class="facts">' +
      '<dt>' + A.esc(t('quakes.region')) + '</dt><dd>' + A.esc(String(region)) + '</dd>' +
      '<dt>' + A.esc(t('quakes.depth')) + '</dt><dd>' + (depth != null ? A.esc(String(depth)) + ' km' : '—') + '</dd>' +
      '<dt>' + A.esc(t('quakes.distance')) + '</dt><dd>' + (dist != null ? A.esc(Math.round(dist) + ' ' + t('quakes.km')) : '—') + '</dd>' +
      '<dt>' + A.esc(t('quakes.time_local')) + '</dt><dd>' + A.esc(tm ? A.fmtDateTime(tm) : '—') + '</dd>' +
      '<dt>' + A.esc(t('quakes.time_utc')) + '</dt><dd>' + A.esc(tm ? A.fmtUTC(tm) : '—') + '</dd>' +
      '<dt>' + A.esc(t('quakes.felt')) + '</dt><dd>' + (felt != null ? A.esc(String(felt)) : '—') + '</dd>' +
      '<dt>' + A.esc(t('quakes.tsunami')) + '</dt><dd>' + (tsu != null ? A.esc(String(tsu)) : '—') + '</dd>' +
      '<dt>' + A.esc(t('common.source')) + '</dt><dd>' + (src != null ? A.esc(String(src)) : '—') + '</dd>' +
      '<dt>' + A.esc(t('tsunami.last_update')) + '</dt><dd>' + A.esc(upd ? A.fmtDateTime(upd) : '—') + '</dd>' +
      '</dl>' +
      (pct != null ? '<div class="conf"><span>' + A.esc(t('quakes.confidence')) + '</span>' +
        '<span class="conf-bar" role="img" aria-label="' + A.esc(pct + '%') + '"><span class="conf-fill" style="width:' + pct + '%"></span></span>' +
        '<span>' + pct + '%</span></div>' +
        (reasons.length ? '<details class="why"><summary>' + A.esc(t('home.confidence.why')) + '</summary><ul>' +
          reasons.map(r => '<li>' + A.esc(r) + '</li>').join('') + '</ul></details>' : '') : '') +
      (id != null ? '<p class="map-note">ID: ' + A.esc(String(id)) + '</p>' : '') +
      (id != null ? '<button type="button" class="btn btn-small" data-quake-detail="' + A.esc(String(id)) + '">' +
        A.esc(t('qx.details')) + '</button><div class="quake-detail" id="qd-' + A.esc(String(id)).replace(/[^a-zA-Z0-9_-]/g, '') + '"></div>' : '') +
      '</article>';
  }

  /* Original → Revised → Latest fusion timeline from revision history. */
  function revisionTimeline(revs) {
    if (!Array.isArray(revs) || !revs.length) {
      return '<p class="map-note">' + A.esc(t('qx.no_revisions')) + '</p>';
    }
    const sorted = revs.slice().sort((a, b) =>
      new Date(A.pick(a, ['received_at', 'update_time']) || 0) - new Date(A.pick(b, ['received_at', 'update_time']) || 0));
    const labels = [t('qx.original')];
    for (let i = 1; i < sorted.length; i++) labels.push(t('qx.revised') + ' ' + i);
    labels[labels.length - 1] = t('qx.latest') + (sorted.length > 1 ? ' (' + t('qx.revised') + ' ' + (sorted.length - 1) + ')' : '');
    return '<h4>' + A.esc(t('qx.revisions')) + '</h4><ul class="timeline">' + sorted.map((r, i) => {
      const mag = A.pick(r, ['magnitude']), dep = A.pick(r, ['depth']);
      const tm = A.pick(r, ['origin_time']);
      const received = A.pick(r, ['received_at', 'update_time']);
      const st = A.pick(r, ['status']);
      return '<li><strong>' + A.esc(labels[i]) + '</strong> ' + A.kindTag('detection') +
        '<div class="t-time">' + A.esc(received ? A.fmtDateTime(received) : '—') + '</div>' +
        '<div class="t-body">M ' + A.esc(mag != null ? String(mag) : '—') +
        ' · ' + A.esc(t('quakes.depth')) + ' ' + A.esc(dep != null ? String(dep) + ' km' : '—') +
        (tm ? '<br>' + A.esc(A.fmtUTC(tm)) : '') +
        (st ? '<br><span class="map-note">' + A.esc(String(st)) + '</span>' : '') +
        '</div></li>';
    }).join('') + '</ul>';
  }

  function aftershockBlock(as) {
    if (!as || as.status !== 'ok') return '';
    let html = '<h4>' + A.esc(t('qx.aftershock')) + ' ' + A.kindTag('probability') + '</h4>';
    html += '<div class="note" role="note">ℹ️ ' + A.esc(t('qx.aftershock_disclaimer')) + '</div>';
    html += '<dl class="facts">' +
      '<dt>' + A.esc(t('qx.aftershock_largest')) + '</dt><dd>M ' + A.esc(String(as.expected_largest_aftershock_typical)) + ' (' + A.esc(t('qx.aftershock_typical')) + ')</dd>' +
      '<dt>' + A.esc(t('qx.aftershock_rate')) + '</dt><dd>' + A.esc(String(as.rate_now_relative_to_1h)) + '×</dd></dl>';
    const wins = Array.isArray(as.windows) ? as.windows : [];
    if (wins.length) {
      html += '<div class="grid3">' + wins.map(w =>
        '<div class="stat"><div class="v">' + A.esc(Math.round((w.probability_at_least_one_M5plus || 0) * 100) + '%') +
        '</div><div class="k">' + A.esc(String(w.window)) + '</div></div>').join('') + '</div>';
    }
    return html;
  }

  async function loadDetail(id, el) {
    const key = String(id);
    if (detailsCache[key] === 'loading') return;
    if (detailsCache[key]) { el.innerHTML = detailsCache[key]; return; }
    detailsCache[key] = 'loading';
    el.innerHTML = '<div class="skeleton" style="height:80px"></div>';
    try {
      const [dq, da] = await Promise.all([
        A.api.request('/earthquakes/' + encodeURIComponent(key)).catch(() => null),
        A.api.request('/engines/aftershock?event_id=' + encodeURIComponent(key)).catch(() => null)
      ]);
      let html = '';
      if (dq && dq.data) html += revisionTimeline(A.pick(dq.data, ['revisions', 'updates', 'history']));
      if (da && da.data) html += aftershockBlock(da.data);
      if (!html) html = '<p class="map-note">' + A.esc(t('common.unavailable.title')) + '</p>';
      detailsCache[key] = html;
      el.innerHTML = html;
    } catch (e) {
      detailsCache[key] = null;
      el.innerHTML = '<p class="map-note">' + A.esc(t('common.unavailable.title')) + '</p>';
    }
  }

  async function render(host) {
    host.innerHTML = '<div class="card"><h2>' + A.esc(t('quakes.title')) + '</h2><div class="skeleton" style="height:160px"></div></div>';
    const res = await A.api.request('/earthquakes');
    if (!res.data) {
      host.innerHTML = '<div class="card"><h2>' + A.esc(t('quakes.title')) + '</h2>' +
        A.emptyState('common.unavailable.title', 'common.unavailable.desc') + '</div>';
      return;
    }
    let list = A.pick(res.data, ['earthquakes', 'events', 'data']) || [];
    if (!Array.isArray(list)) list = [];
    list.sort((a, b) => new Date(A.pick(b, ['time', 'occurred_at', 'origin_time']) || 0) - new Date(A.pick(a, ['time', 'occurred_at', 'origin_time']) || 0));

    let html = '<div class="card"><h2>' + A.esc(t('quakes.title')) + ' · ' + A.esc(A.store.active.name) + '</h2>';
    if (!list.length) html += A.emptyState('quakes.no_data', 'common.unavailable.desc');
    html += A.updatedLine(res.data, res) + '</div>';
    html += list.slice(0, 40).map(cardHTML).join('');
    host.innerHTML = html;
    host.querySelectorAll('[data-quake-detail]').forEach(btn =>
      btn.addEventListener('click', () => {
        const id = btn.getAttribute('data-quake-detail');
        const el = host.querySelector('#qd-' + id.replace(/[^a-zA-Z0-9_-]/g, ''));
        if (el) loadDetail(id, el);
      }));
    A.i18n.applyStatic(host);
  }

  A.views.quakes = { render };
})();
