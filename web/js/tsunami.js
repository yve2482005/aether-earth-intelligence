/* AETHER Tsunami (v2) — official status per saved location + message history.
   Only official messages are shown; raw_classification is always preserved.
   Message versioning: v1 → v2 → … → CANCELLED from tsunami_messages.version
   (falls back to sequence order when version is NULL for pre-versioning rows).
   "Arrival estimate unavailable." is shown when the API provides none.
   No predictive language anywhere. */
(function () {
  'use strict';
  const A = window.AETHER;
  const t = (k, v) => A.i18n.t(k, v);

  // Official categories: original English + localized explanation, side by side.
  function categoryLabel(cat) {
    const c = String(cat || 'INFORMATION').toUpperCase().replace(/[^A-Z_ ]/g, '').trim() || 'INFORMATION';
    const key = 'tsunami.cat.' + c.replace(/ /g, '_');
    const desc = t(key + '.desc');
    const hasDesc = desc !== key + '.desc';
    return '<span class="sev ' + (c === 'WARNING' ? 'sev-critical' : c === 'ADVISORY' ? 'sev-warning' : 'sev-info') + '">' +
      A.esc(c) + '</span>' + (hasDesc ? ' <span>' + A.esc(desc) + '</span>' : '');
  }

  function statusFor(loc, data) {
    const per = A.pick(data, ['by_location', 'status_by_location', 'locations']) || [];
    if (Array.isArray(per)) {
      const hit = per.find(s => String(A.pick(s, ['location_id', 'id']) || '') === loc.id ||
        (A.pick(s, ['lat']) === loc.lat && A.pick(s, ['lon']) === loc.lon));
      if (hit) return hit;
    }
    return A.pick(data, ['status', 'current']) || null;
  }

  /* v1 → v2 → … → CANCELLED version chain for one event's messages. */
  function versionChain(msgs, eventStatus) {
    const msgTime = m => A.pick(m, ['issue_time', 'received_at']);
    const sorted = msgs.slice().sort((a, b) => {
      const va = A.pick(a, ['version']), vb = A.pick(b, ['version']);
      if (va != null && vb != null && va !== vb) return va - vb;
      return new Date(msgTime(a) || 0) - new Date(msgTime(b) || 0);
    });
    let html = '<ol class="versions">';
    sorted.forEach((m, i) => {
      const v = A.pick(m, ['version']);
      const label = 'v' + (v != null ? v : (i + 1));
      const cat = A.pick(m, ['raw_classification', 'message_type', 'category']);
      const issued = A.pick(m, ['issue_time', 'received_at']);
      const src = A.pick(m, ['source', 'agency']);
      html += '<li><span class="ver">' + A.esc(label) + '</span> ' + categoryLabel(cat) +
        '<div class="t-time">' + A.esc(issued ? A.fmtDateTime(issued) : '—') + '</div>' +
        '<div class="t-body"><span class="map-note">' + A.esc(t('tsunami.raw')) + ': <code>' +
        A.esc(String(cat || '—')) + '</code>' + (src ? ' · ' + A.esc(t('common.source')) + ': ' + A.esc(String(src)) : '') +
        '</span></div></li>';
    });
    const st = String(eventStatus || '').toLowerCase();
    if (st === 'cancelled') {
      html += '<li><span class="ver ver-cancel">' + A.esc(t('tsx.cancelled')) + '</span> ' +
        '<span class="map-note">' + A.esc(t('tsx.cancelled_note')) + '</span></li>';
    }
    html += '</ol>';
    return html;
  }

  async function render(host) {
    host.innerHTML = '<div class="card"><h2>' + A.esc(t('tsunami.title')) + '</h2><div class="skeleton" style="height:160px"></div></div>';
    const res = await A.api.request('/tsunami');
    if (!res.data) {
      host.innerHTML = '<div class="card"><h2>' + A.esc(t('tsunami.title')) + '</h2>' +
        '<div class="note" role="note">ℹ️ ' + A.esc(t('tsunami.unavailable')) + '</div>' +
        A.updatedLine({}, res) + '</div>';
      return;
    }
    const data = res.data;
    let html = '<div class="card"><div style="display:flex;justify-content:space-between;align-items:center;gap:8px">' +
      '<h2>' + A.esc(t('tsunami.title')) + '</h2>' + A.kindTag('warning') + '</div>';

    // status per saved location
    html += '<h3>' + A.esc(t('tsunami.status')) + '</h3>';
    A.store.all.forEach(loc => {
      const st = statusFor(loc, data);
      const cat = st ? A.pick(st, ['raw_classification', 'category', 'classification']) : null;
      const arrival = st ? A.pick(st, ['arrival_estimate', 'arrival_time', 'eta']) : null;
      const issued = st ? A.pick(st, ['issued_at', 'issue_time']) : null;
      const src = st ? A.pick(st, ['source', 'agency']) : null;
      const updated = st ? A.pick(st, ['updated_at', 'last_update']) : null;
      html += '<div class="alert sev-info"><h3>📍 ' + A.esc(loc.name) + '</h3>' +
        '<p>' + (cat ? categoryLabel(cat) : A.esc(t('tsunami.unavailable'))) + '</p>' +
        '<dl class="facts">' +
        '<dt>' + A.esc(t('tsunami.arrival')) + '</dt><dd>' + (arrival ? A.esc(A.fmtDateTime(arrival)) : A.esc(t('tsunami.arrival_unavailable'))) + '</dd>' +
        '<dt>' + A.esc(t('tsunami.issue_time')) + '</dt><dd>' + A.esc(issued ? A.fmtDateTime(issued) : '—') + '</dd>' +
        '<dt>' + A.esc(t('common.source')) + '</dt><dd>' + A.esc(src != null ? String(src) : '—') + '</dd>' +
        '<dt>' + A.esc(t('tsunami.last_update')) + '</dt><dd>' + A.esc(updated ? A.fmtDateTime(updated) : '—') + '</dd>' +
        '</dl></div>';
    });

    // message history with versioning — fetch detail per event for messages
    const events = A.pick(data, ['events', 'messages', 'history', 'data']) || [];
    const evList = Array.isArray(events) ? events : [];
    html += '<h3>' + A.esc(t('tsunami.history')) + '</h3><div id="tsu-versions"><div class="skeleton" style="height:80px"></div></div>';
    html += A.updatedLine(data, res) + '</div>';
    host.innerHTML = html;

    const box = host.querySelector('#tsu-versions');
    if (!evList.length) {
      box.innerHTML = A.emptyState('tsunami.no_messages', 'common.unavailable.desc');
    } else {
      const details = await Promise.all(evList.slice(0, 5).map(ev => {
        const eid = A.pick(ev, ['tsunami_event_id', 'id', 'event_id']);
        if (!eid) return Promise.resolve(null);
        return A.api.request('/tsunami/' + encodeURIComponent(String(eid)))
          .then(r => ({ ev, detail: r.data })).catch(() => ({ ev, detail: null }));
      }));
      let vhtml = '';
      details.forEach(d => {
        if (!d) return;
        const ev = d.ev, det = d.detail || {};
        const msgs = A.pick(det, ['messages', 'history']) || [];
        const eid = A.pick(ev, ['tsunami_event_id', 'id', 'event_id']);
        const region = A.pick(ev, ['affected_region', 'region']) || '';
        vhtml += '<div class="card"><h4>' + A.esc(String(eid || '')) + (region ? ' · ' + A.esc(String(region)) : '') + '</h4>';
        if (Array.isArray(msgs) && msgs.length) vhtml += versionChain(msgs, A.pick(ev, ['status']) || A.pick(det, ['status']));
        else vhtml += '<p class="map-note">' + A.esc(t('tsunami.no_messages')) + '</p>';
        vhtml += '</div>';
      });
      box.innerHTML = vhtml || A.emptyState('tsunami.no_messages', 'common.unavailable.desc');
    }
    A.i18n.applyStatic(host);
  }

  A.views.tsunami = { render };
})();
