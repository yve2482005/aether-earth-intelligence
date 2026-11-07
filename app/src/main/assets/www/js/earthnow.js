/* AETHER Earth Now (v2) — unified event feed from /v1/events.
   Filters: All / Nearby / Severe / Earthquake / Tsunami / Weather / Storm / Flood / AQI.
   Every card shows: event ID, type, status, severity, location, detection time,
   last update, source, source timestamp, confidence, data age, distance from
   user, official status (joined from /v1/alerts by event_id).
   New event kinds (storm/flood/volcano/wildfire/lightning) appear ONLY when
   real data supports them — otherwise an honest empty state, never fake events. */
(function () {
  'use strict';
  const A = window.AETHER;
  const t = (k, v) => A.i18n.t(k, v);

  const FILTERS = [
    ['all', null], ['nearby', 'nearby'], ['severe', 'severe'],
    ['earthquake', 'EARTHQUAKE'], ['tsunami', 'TSUNAMI'], ['weather', 'WEATHER'],
    ['storm', 'CYCLONE'], ['flood', 'FLOOD'], ['aqi', 'AQI']
  ];
  let activeFilter = 'all';

  function isNearby(ev) {
    const elat = A.pick(ev, ['lat', 'latitude']), elon = A.pick(ev, ['lon', 'longitude']);
    if (elat == null || elon == null) return false;
    return A.store.all.some(l => { const d = A.haversineKm(l.lat, l.lon, elat, elon); return d != null && d <= 500; });
  }

  function isSevere(ev) {
    const s = String(A.pick(ev, ['severity']) || '').toLowerCase();
    return ['severe', 'critical', 'warning', 'high', 'extreme'].includes(s);
  }

  function matches(ev, f) {
    if (f === 'all') return true;
    if (f === 'nearby') return isNearby(ev);
    if (f === 'severe') return isSevere(ev);
    return String(A.pick(ev, ['event_type', 'type']) || '').toUpperCase() === f;
  }

  function cardHTML(ev, alertKind) {
    const loc = A.store.active;
    const id = A.pick(ev, ['event_id', 'id']) || '—';
    const type = A.pick(ev, ['event_type', 'type']) || '—';
    const kind = A.pick(ev, ['kind']) || 'observation';
    const status = A.pick(ev, ['status']) || '—';
    const sev = A.pick(ev, ['severity']);
    const region = A.pick(ev, ['region', 'place']);
    const elat = A.pick(ev, ['lat', 'latitude']), elon = A.pick(ev, ['lon', 'longitude']);
    const start = A.pick(ev, ['start_time', 'time', 'occurred_at']);
    const updated = A.pick(ev, ['updated_at', 'last_update']);
    const src = A.pick(ev, ['source', 'agency']);
    const srcTs = A.pick(ev, ['created_at', 'source_timestamp']);
    const conf = A.pick(ev, ['confidence']);
    const confReason = A.pick(ev, ['confidence_reason']);
    const dist = A.haversineKm(loc.lat, loc.lon, elat, elon);
    const ageMin = A.minutesSince(updated || start);
    const where = region || (elat != null && elon != null ? elat.toFixed(2) + ', ' + elon.toFixed(2) : '—');

    return '<article class="card now-card">' +
      '<div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap">' +
      '<strong>' + A.esc(String(type)) + '</strong>' +
      '<span>' + A.kindTag(kind) + ' ' + (sev ? A.sevBadge(sev) : '') + '</span></div>' +
      '<dl class="facts">' +
      '<dt>' + A.esc(t('now.id')) + '</dt><dd><code>' + A.esc(String(id)) + '</code></dd>' +
      '<dt>' + A.esc(t('now.location')) + '</dt><dd>' + A.esc(String(where)) + '</dd>' +
      '<dt>' + A.esc(t('now.detected')) + '</dt><dd>' + A.esc(start ? A.fmtDateTime(start) : '—') + '</dd>' +
      '<dt>' + A.esc(t('now.last_update')) + '</dt><dd>' + A.esc(updated ? A.fmtDateTime(updated) : '—') + '</dd>' +
      '<dt>' + A.esc(t('common.source')) + '</dt><dd>' + A.esc(src != null ? String(src) : '—') + '</dd>' +
      '<dt>' + A.esc(t('now.source_time')) + '</dt><dd>' + A.esc(srcTs ? A.fmtDateTime(srcTs) : '—') + '</dd>' +
      '<dt>' + A.esc(t('home.confidence')) + '</dt><dd>' + A.esc(conf != null ? String(conf) : '—') +
      (confReason ? ' <span class="map-note">(' + A.esc(String(confReason)) + ')</span>' : '') + '</dd>' +
      '<dt>' + A.esc(t('now.data_age')) + '</dt><dd>' + A.esc(ageMin != null ? t('common.updated_ago', { min: ageMin }) : '—') + '</dd>' +
      '<dt>' + A.esc(t('quakes.distance')) + '</dt><dd>' + A.esc(dist != null ? Math.round(dist) + ' ' + t('quakes.km') : '—') + '</dd>' +
      '<dt>' + A.esc(t('now.status')) + '</dt><dd>' + A.esc(String(status)) + '</dd>' +
      '<dt>' + A.esc(t('now.official')) + '</dt><dd>' + A.esc(alertKind || '—') + '</dd>' +
      '</dl></article>';
  }

  function filterBar() {
    return '<div class="filterbar" role="toolbar" aria-label="' + A.esc(t('alerts.filter')) + '">' +
      FILTERS.map(([key, val]) =>
        '<button type="button" class="chip' + (activeFilter === key ? ' on' : '') + '" data-filter="' + key + '">' +
        A.esc(t('now.filter.' + key)) + '</button>').join('') + '</div>';
  }

  async function render(host) {
    host.innerHTML = '<div class="card"><h2>' + A.esc(t('earthnow.title')) + '</h2><div class="skeleton" style="height:160px"></div></div>';
    const [res, alertsRes] = await Promise.all([
      A.api.request('/events'),
      A.api.request('/alerts').catch(() => null)
    ]);
    if (!res.data) {
      host.innerHTML = '<div class="card"><h2>' + A.esc(t('earthnow.title')) + '</h2>' +
        A.emptyState('common.unavailable.title', 'common.unavailable.desc') + '</div>';
      return;
    }
    let list = A.pick(res.data, ['events', 'items', 'data']) || [];
    if (!Array.isArray(list)) list = [];
    list.sort((a, b) => new Date(A.pick(b, ['start_time', 'time', 'occurred_at', 'updated_at']) || 0) -
                                new Date(A.pick(a, ['start_time', 'time', 'occurred_at', 'updated_at']) || 0));

    const alertKindByEvent = {};
    const al = alertsRes && alertsRes.data ? (A.pick(alertsRes.data, ['alerts', 'items', 'data']) || []) : [];
    (Array.isArray(al) ? al : []).forEach(a => {
      const eid = A.pick(a, ['event_id']);
      if (eid) alertKindByEvent[String(eid)] = A.pick(a, ['kind', 'kind_detail']);
    });

    let html = '<div class="card"><h2>' + A.esc(t('earthnow.title')) + '</h2>' +
      '<p class="map-note">' + A.esc(t('earthnow.subtitle')) + '</p>' + filterBar() +
      '<div id="now-list"></div>' + A.updatedLine(res.data, res) + '</div>';
    host.innerHTML = html;

    const listEl = host.querySelector('#now-list');
    const draw = () => {
      const items = list.filter(ev => matches(ev, activeFilter));
      if (!items.length) {
        const emptyKey = activeFilter === 'all' ? 'earthnow.no_events' : 'now.no_match';
        listEl.innerHTML = A.emptyState(emptyKey + '.title', emptyKey + '.desc');
      } else {
        listEl.innerHTML = items.slice(0, 40).map(ev =>
          cardHTML(ev, alertKindByEvent[String(A.pick(ev, ['event_id', 'id']) || '')])).join('');
      }
      host.querySelectorAll('[data-filter]').forEach(b =>
        b.classList.toggle('on', b.getAttribute('data-filter') === activeFilter));
    };
    host.querySelectorAll('[data-filter]').forEach(b =>
      b.addEventListener('click', () => { activeFilter = b.getAttribute('data-filter'); draw(); }));
    draw();
    A.i18n.applyStatic(host);
  }

  A.views.earthnow = { render };
})();
