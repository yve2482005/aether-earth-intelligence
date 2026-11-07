/* AETHER Alerts — unified alert center.
   Priority enforced: official life-safety alerts first, then by severity,
   then newest. Severity filters, quiet-hours setting, dedup note.
   renderPinned() feeds the Safety-Mode banner. */
(function () {
  'use strict';
  const A = window.AETHER;
  const t = (k, v) => A.i18n.t(k, v);
  let filter = 'all';
  let lastAlerts = [];

  /* === AETHER v2-C: alert state machine + expert/safety upgrades (workstream C) === */
  const V2_RANK = { info: 0, information: 0, advisory: 1, low: 1, watch: 2, moderate: 2, warning: 3, high: 3, critical: 4, severe: 4, life_safety: 5 };
  function v2rank(sev) { return V2_RANK[String(sev || 'info').toLowerCase()] || 0; }
  function expertOn() { return !!A.settings.get('expertMode'); }
  function safetyOn() { return !!(A.safety && A.safety.isOn()); }
  function isLifeSafety(a) {
    return !!A.pick(a, ['life_safety', 'official_life_safety']) ||
      v2rank(A.pick(a, ['severity'])) >= 4;
  }
  /* Fill revision counts + last-update times on visible cards (state machine §12). */
  async function enrichCards(host) {
    const cards = host.querySelectorAll('article.alert[data-alert-id]');
    for (const card of cards) {
      const id = card.getAttribute('data-alert-id');
      const revEl = card.querySelector('.a-rev');
      if (!revEl || revEl.dataset.done) continue;
      try {
        const res = await A.api.request('/notify/alerts/' + encodeURIComponent(id) + '/revisions');
        const revs = (res.data && (res.data.revisions || [])) || [];
        revEl.dataset.done = '1';
        if (revs.length) {
          const last = revs[revs.length - 1];
          revEl.innerHTML = A.esc(t('alx.revisions') + ': ' + revs.length +
            ' · ' + t('alx.state') + ': ' + String(last.to_state || '').toLowerCase() +
            ' · ' + t('tsunami.last_update') + ': ' + A.fmtDateTime(last.changed_at));
        } else {
          revEl.textContent = t('alx.no_changes');
        }
        if (expertOn()) {
          const xEl = card.querySelector('.a-expert');
          if (xEl && !xEl.dataset.done) {
            xEl.dataset.done = '1';
            const det = await A.api.request('/notify/alerts/' + encodeURIComponent(id));
            const d = (det.data && det.data.alert) || {};
            xEl.innerHTML = '<div class="expert-box"><strong>' + A.esc(t('xp.title')) + '</strong>' +
              '<br>' + A.esc(t('alx.version') + ': ' + (d.version || 1) +
              ' · ' + t('alx.confidence') + ': ' + (d.confidence || '—') +
              ' · ' + t('common.source') + ': ' + (d.source || '—')) +
              '<br>' + A.esc(t('xp.raw') + ': ') +
              '<code>' + A.esc(JSON.stringify({ severity: d.severity, priority: d.priority, kind: d.kind, dedup_key: d.dedup_key, notification_status: d.notification_status }).slice(0, 400)) + '</code>' +
              (revs.length ? '<br>' + A.esc(t('xp.revisions') + ': ') +
                revs.map(r => A.esc(r.from_state + '→' + r.to_state + ' (' + (r.reason || '') + ')')).join('; ') : '') +
              '</div>';
          }
        }
      } catch (e) { /* offline: leave placeholder */ }
    }
  }
  /* Notification history (read/unread) — backend/notify/history.py. */
  function apiBase() { return ((A.config && A.config.apiBase) || '') + '/v1'; }
  async function apiPost(path) {
    const res = await fetch(apiBase() + path, { method: 'POST', headers: { Accept: 'application/json' } });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    return res.json();
  }
  async function renderHistory(host) {
    const box = host.querySelector('#ntf-history');
    if (!box) return;
    try {
      const res = await A.api.request('/notify/history?limit=20');
      const items = (res.data && res.data.items) || [];
      const unread = (res.data && res.data.unread) || 0;
      if (!items.length) { box.innerHTML = '<p class="map-note">' + A.esc(t('ntf.no_history')) + '</p>'; return; }
      box.innerHTML = '<p class="map-note">' + A.esc(t('ntf.unread') + ': ' + unread) + '</p><ul class="timeline">' +
        items.map(n =>
          '<li><div class="t-body">' + (n.read_at ? '' : '<strong>● </strong>') +
          '<strong>' + A.esc(String(n.title || '—')) + '</strong> ' +
          '<span class="map-note">' + A.esc(n.status || '') + ' · ' + A.esc(n.created_at ? A.fmtDateTime(n.created_at) : '') + '</span>' +
          (n.read_at ? '' : ' <button type="button" class="btn btn-small" data-nread="' + n.id + '">' + A.esc(t('ntf.mark_read')) + '</button>') +
          '</div></li>').join('') + '</ul>';
      box.querySelectorAll('[data-nread]').forEach(b => b.addEventListener('click', async () => {
        try { await apiPost('/notify/history/' + b.dataset.nread + '/read'); } catch (e) { /* offline */ }
        renderHistory(host);
      }));
    } catch (e) { box.innerHTML = '<p class="map-note">' + A.esc(t('common.unavailable.title')) + '</p>'; }
  }

  function rank(a) {
    const sev = String(A.pick(a, ['severity']) || 'info').toLowerCase();
    const lifeSafety = !!A.pick(a, ['life_safety', 'official_life_safety']);
    return [lifeSafety ? 0 : 1, sev === 'critical' ? 0 : sev === 'warning' ? 1 : 2,
      -(new Date(A.pick(a, ['issued_at', 'time', 'updated_at']) || 0).getTime())];
  }
  function cmp(a, b) {
    const ra = rank(a), rb = rank(b);
    for (let i = 0; i < ra.length; i++) if (ra[i] !== rb[i]) return ra[i] - rb[i];
    return 0;
  }

  function quietActive() {
    const f = A.settings.get('quietFrom') || '22:00', to = A.settings.get('quietTo') || '06:00';
    const now = new Date(); const cur = now.getHours() * 60 + now.getMinutes();
    const p = s => { const [h, m] = String(s).split(':').map(Number); return h * 60 + (m || 0); };
    const pf = p(f), pt = p(to);
    return pf <= pt ? (cur >= pf && cur < pt) : (cur >= pf || cur < pt);
  }

  function alertCard(a) {
    const sev = String(A.pick(a, ['severity']) || 'info').toLowerCase();
    const title = A.pick(a, ['title', 'headline']) || '—';
    const desc = A.pick(a, ['description', 'summary', 'message']);
    const issued = A.pick(a, ['issued_at', 'time']);
    const src = A.pick(a, ['source', 'agency']);
    const kind = A.pick(a, ['kind']) || 'warning';
    const dup = A.pick(a, ['merged_count', 'duplicates']);
    const aid = A.pick(a, ['alert_id']) || '';
    const lifeSafety = isLifeSafety(a);
    const sevCls = sev === 'critical' ? 'critical' : sev === 'warning' ? 'warning' : 'info';
    /* §15 safety mode: strong visual state for critical/warning, official source prominent */
    const safetyCls = (safetyOn() && (sev === 'critical' || sev === 'warning' || lifeSafety)) ? ' safety-critical' : '';
    let html = '<article class="alert sev-' + sevCls + safetyCls + '"' +
      (aid ? ' data-alert-id="' + A.esc(String(aid)) + '"' : '') + '>' +
      '<h3>' + (lifeSafety ? '🛟 ' : '') + A.esc(String(title)) + '</h3>' +
      '<p>' + A.sevBadge(sev) + ' ' + A.kindTag(kind) + '</p>' +
      (desc ? '<p>' + A.esc(String(desc)) + '</p>' : '') +
      '<p class="a-meta">' + A.esc(t('alerts.issued')) + ': ' + A.esc(issued ? A.fmtDateTime(issued) : '—') +
      ' · ' + A.esc(t('common.source')) + ': ' + A.esc(src != null ? String(src) : '—') +
      (dup ? ' · ' + A.esc(t('alerts.dedup') + ' ×' + dup) : '') + '</p>' +
      '<p class="a-meta a-rev">' + A.esc(t('alx.revisions')) + ': …</p>' +
      /* §15: official recommended actions ONLY when from an official authority */
      (kind === 'official'
        ? '<p class="a-official">🏛️ <strong>' + A.esc(t('alx.official_actions')) + '</strong> — ' +
          A.esc(t('alx.official_only')) + ' (' + A.esc(src != null ? String(src) : '—') + ')</p>'
        : '') +
      (expertOn() ? '<div class="a-expert"></div>' : '') +
      '</article>';
    return html;
  }

  function renderPinned() {
    const box = document.getElementById('safety-pinned-list');
    if (!box) return;
    const act = lastAlerts.filter(a => String(A.pick(a, ['severity']) || '').toLowerCase() !== 'info').slice(0, 5);
    box.innerHTML = act.length ? act.map(alertCard).join('')
      : '<p class="map-note">' + A.esc(t('alerts.no_alerts')) + '</p>';
  }

  async function render(host) {
    host.innerHTML = '<div class="card"><h2>' + A.esc(t('alerts.title')) + '</h2><div class="skeleton" style="height:140px"></div></div>';
    const res = await A.api.request('/alerts');
    if (!res.data) {
      host.innerHTML = '<div class="card"><h2>' + A.esc(t('alerts.title')) + '</h2>' +
        A.emptyState('common.unavailable.title', 'common.unavailable.desc') + '</div>';
      return;
    }
    let list = A.pick(res.data, ['alerts', 'items', 'data']) || [];
    if (!Array.isArray(list)) list = [];
    list.sort(cmp);
    lastAlerts = list;

    const qa = quietActive();
    const sOn = safetyOn();
    const shown = list.filter(a => {
      const sev = String(A.pick(a, ['severity']) || 'info').toLowerCase();
      if (filter !== 'all' && sev !== filter) return false;
      /* §15: in safety mode, critical/life-safety alerts bypass quiet filtering */
      if (qa && A.settings.get('criticalOnly') && sev !== 'critical' && !(sOn && isLifeSafety(a))) return false;
      return true;
    });

    let html = '<div class="card"><h2>' + A.esc(t('alerts.title')) + '</h2>' +
      '<p class="map-note">ℹ️ ' + A.esc(t('alerts.priority')) + '<br>ℹ️ ' + A.esc(t('alerts.dedup_note')) + '</p>' +
      '<div class="filters" role="group" aria-label="' + A.esc(t('alerts.filter')) + '">' +
      ['all', 'critical', 'warning', 'info'].map(f =>
        '<button type="button" class="btn btn-small" data-filter="' + f + '" aria-pressed="' + String(filter === f) + '">' +
        A.esc(t(f === 'all' ? 'alerts.all' : 'alerts.sev.' + f)) + '</button>').join('') + '</div>';

    // quiet hours
    html += '<div class="card" style="box-shadow:none"><h3>🔇 ' + A.esc(t('alerts.quiet')) + '</h3>' +
      (qa ? '<p><span class="sev sev-info">' + A.esc(t('alerts.quiet.active')) + '</span></p>' : '') +
      '<div class="grid2"><label class="field"><span>' + A.esc(t('alerts.quiet.from')) + '</span>' +
      '<input type="text" id="q-from" value="' + A.esc(A.settings.get('quietFrom')) + '" inputmode="numeric" placeholder="22:00"></label>' +
      '<label class="field"><span>' + A.esc(t('alerts.quiet.to')) + '</span>' +
      '<input type="text" id="q-to" value="' + A.esc(A.settings.get('quietTo')) + '" inputmode="numeric" placeholder="06:00"></label></div>' +
      '<label class="switch"><input type="checkbox" id="q-crit" ' + (A.settings.get('criticalOnly') ? 'checked' : '') + '>' +
      '<span class="switch-track" aria-hidden="true"></span><span class="switch-label">' + A.esc(t('settings.notif.critical_only')) + '</span></label></div>';

    if (!shown.length) html += A.emptyState('alerts.no_alerts', 'alerts.dedup_note');
    else html += '<div class="alert-list">' + shown.slice(0, 50).map(alertCard).join('') + '</div>';

    /* === v2-C: notification history (workstream C) === */
    html += '<div class="card" style="box-shadow:none"><h3>📬 ' + A.esc(t('ntf.title')) + '</h3>' +
      '<div id="ntf-history"><p class="map-note">…</p></div></div>';

    html += A.updatedLine(res.data, res) + '</div>';

    host.innerHTML = html;
    A.i18n.applyStatic(host);
    host.querySelectorAll('[data-filter]').forEach(b => b.addEventListener('click', () => { filter = b.dataset.filter; render(host); }));
    host.querySelector('#q-from').addEventListener('change', e => A.settings.set('quietFrom', e.target.value));
    host.querySelector('#q-to').addEventListener('change', e => A.settings.set('quietTo', e.target.value));
    host.querySelector('#q-crit').addEventListener('change', e => { A.settings.set('criticalOnly', e.target.checked); render(host); });
    renderPinned();
    enrichCards(host);   /* v2-C: revision counts + last-update + expert details */
    renderHistory(host); /* v2-C: notification history */
  }

  A.views.alerts = { render, renderPinned };
})();
