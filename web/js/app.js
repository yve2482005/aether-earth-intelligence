/* AETHER core — router, API client, offline cache, locations, settings.
   Every fetch: timeout via AbortController; on failure, serve last-good
   localStorage payload with visible staleness; else honest empty state.
   No number is ever invented: missing data renders as "unavailable". */
(function () {
  'use strict';
  const A = window.AETHER;
  const t = (k, v) => A.i18n.t(k, v);

  /* ---------- settings (local only) ---------- */
  const SETTINGS_KEY = 'aether.settings.v1';
  const settings = {
    data: Object.assign(
      { lang: 'my', units: 'metric', safetyMode: false, theme: 'dark', contrast: false,
        notifEnabled: true, criticalOnly: false, quietFrom: '22:00', quietTo: '06:00' },
      loadJson(SETTINGS_KEY, {})
    ),
    get(k) { return this.data[k]; },
    set(k, v) { this.data[k] = v; saveJson(SETTINGS_KEY, this.data); }
  };
  function loadJson(k, fb) { try { const s = localStorage.getItem(k); return s ? JSON.parse(s) : fb; } catch (e) { return fb; } }
  function saveJson(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* quota: ignore */ } }
  A.settings = settings;

  /* ---------- locations ---------- */
  const LOC_KEY = 'aether.locations.v1';
  const DEFAULT_LOC = { id: 'yangon', name: 'ရန်ကုန်', name_en: 'Yangon', lat: 16.84, lon: 96.17 };
  const store = {
    get all() { const l = loadJson(LOC_KEY, null); return Array.isArray(l) && l.length ? l : [DEFAULT_LOC]; },
    get activeId() { return loadJson('aether.activeLoc.v1', 'yangon'); },
    get active() { const all = this.all; return all.find(l => l.id === this.activeId) || all[0]; },
    setActive(id) { saveJson('aether.activeLoc.v1', id); A.events.emit('location'); },
    add(loc) { const all = this.all.filter(l => l.id !== loc.id); all.push(loc); saveJson(LOC_KEY, all); A.events.emit('location'); },
    remove(id) { const all = this.all.filter(l => l.id !== id); if (!all.length) return; saveJson(LOC_KEY, all);
      if (this.activeId === id) this.setActive(all[0].id); else A.events.emit('location'); }
  };
  A.store = store;

  /* ---------- tiny event bus ---------- */
  const listeners = {};
  A.events = {
    on(ev, fn) { (listeners[ev] = listeners[ev] || []).push(fn); },
    emit(ev, data) { (listeners[ev] || []).forEach(fn => { try { fn(data); } catch (e) { console.error(e); } }); }
  };

  /* ---------- offline cache: last-good payloads ---------- */
  const CACHE_PREFIX = 'aether.cache.v1.';
  const cache = {
    key(path) { return CACHE_PREFIX + path.replace(/[^a-zA-Z0-9]/g, '_').slice(0, 120); },
    put(path, payload) {
      try {
        localStorage.setItem(this.key(path), JSON.stringify({ stored_at: new Date().toISOString(), payload }));
        this.prune();
      } catch (e) { /* ignore */ }
    },
    get(path) { return loadJson(this.key(path), null); },
    clear() { Object.keys(localStorage).filter(k => k.indexOf(CACHE_PREFIX) === 0).forEach(k => localStorage.removeItem(k)); },
    prune() { // keep newest ~150 entries
      const ks = Object.keys(localStorage).filter(k => k.indexOf(CACHE_PREFIX) === 0);
      if (ks.length <= 150) return;
      const rows = ks.map(k => ({ k, t: (loadJson(k, {}) || {}).stored_at || '' })).sort((a, b) => a.t < b.t ? -1 : 1);
      rows.slice(0, ks.length - 150).forEach(r => localStorage.removeItem(r.k));
    }
  };
  A.cache = cache;

  /* ---------- API client ---------- */
  let _snapshotManifest = null; // null = not loaded yet; false = unavailable
  async function snapshotManifest() {
    if (_snapshotManifest !== null) return _snapshotManifest || null;
    try {
      const r = await fetch('data/snapshot/manifest.json', { cache: 'force-cache' });
      _snapshotManifest = r.ok ? await r.json() : false;
    } catch (e) { _snapshotManifest = false; }
    return _snapshotManifest || null;
  }
  const api = {
    base: '', // same-origin /v1 (backend serves the PWA). Override: AETHER.config.apiBase
    timeoutMs: 10000,
    async request(path) {
      const base = (A.config && A.config.apiBase) || this.base;
      const url = base + '/v1' + path;
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), this.timeoutMs);
      try {
        const res = await fetch(url, { signal: ctrl.signal, headers: { Accept: 'application/json' } });
        clearTimeout(timer);
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const json = await res.json();
        cache.put(path, json);
        A.banner.hideStale();
        return { ok: true, fromCache: false, data: json, error: null };
      } catch (err) {
        clearTimeout(timer);
        const hit = cache.get(path);
        if (hit && hit.payload) {
          const ageMin = minutesSince(hit.stored_at);
          A.banner.showStale(ageMin);
          return { ok: false, fromCache: true, data: hit.payload, error: err, staleMin: ageMin, storedAt: hit.stored_at };
        }
        // Bundled snapshot: real backend responses shipped with the app
        // (scripts/make_snapshot.py). Used only when network and cache both
        // miss. Labeled with its age via the staleness banner.
        try {
          const key = path.replace(/[^a-zA-Z0-9]/g, '_').slice(0, 120);
          const sres = await fetch('data/snapshot/' + key + '.json', { cache: 'force-cache' });
          if (sres.ok) {
            const json = await sres.json();
            const man = await snapshotManifest();
            const ageMin = man && man.generated_at ? minutesSince(man.generated_at) : null;
            A.banner.showStale(ageMin);
            return { ok: false, fromCache: false, fromSnapshot: true, data: json,
                     error: err, staleMin: ageMin, snapshotAt: man && man.generated_at };
          }
        } catch (e2) { /* fall through to honest empty state */ }
        A.banner.showStale(null);
        return { ok: false, fromCache: false, data: null, error: err, staleMin: null };
      }
    }
  };
  A.api = api;

  /* ---------- helpers ---------- */
  function minutesSince(iso) {
    if (!iso) return null;
    const ms = Date.now() - new Date(iso).getTime();
    return ms < 0 ? 0 : Math.round(ms / 60000);
  }
  A.minutesSince = minutesSince;

  function provOf(data) { return (data && data.provenance) || {}; }
  A.provOf = provOf;

  // staleness line: "Updated X min ago" + cached/offline note
  A.updatedLine = function (data, res) {
    const p = provOf(data);
    const age = (p.data_age_min != null) ? p.data_age_min : (res && res.staleMin != null ? res.staleMin : minutesSince(p.updated_at));
    const parts = [];
    if (age != null) parts.push(t('common.updated_ago', { min: age }));
    else if (p.updated_at) parts.push(t('common.updated') + ': ' + fmtDateTime(p.updated_at));
    if (res && !res.ok) parts.push(t('banner.stale_suffix'));
    const stale = res && !res.ok;
    return '<div class="prov' + (stale ? ' stale' : '') + '" role="note">' +
      (stale ? '<span class="stale-dot" aria-hidden="true"></span>' : '') +
      esc(parts.join(' ')) +
      (p.sources && p.sources.length ? ' · ' + esc(t('common.source') + ': ' + p.sources.join(', ')) : '') +
      '</div>';
  };

  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }
  A.esc = esc;

  function el(tag, cls, html) { const e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; }
  A.el = el;

  A.kindTag = function (kind) {
    const k = String(kind || 'observation').toLowerCase();
    const icons = { forecast: '🔮', detection: '📡', observation: '👁️', warning: '⚠️', risk: '🎲' };
    const label = t('kind.' + k, null) === 'kind.' + k ? k.toUpperCase() : t('kind.' + k);
    return '<span class="kind kind-' + esc(k) + '" title="' + esc(t('kind.hint')) + '">' +
      '<span aria-hidden="true">' + (icons[k] || '•') + '</span>' + esc(label) + '</span>';
  };

  A.emptyState = function (titleKey, descKey) {
    return '<div class="empty" role="note"><div class="e-icon" aria-hidden="true">📭</div>' +
      '<p><strong>' + esc(t(titleKey)) + '</strong></p><p>' + esc(t(descKey)) + '</p>' +
      '<button type="button" class="btn" data-action="retry">' + esc(t('common.retry')) + '</button></div>';
  };

  A.sevBadge = function (sev) {
    const s = String(sev || 'info').toLowerCase();
    const key = 'alerts.sev.' + (s === 'critical' ? 'critical' : s === 'warning' ? 'warning' : 'info');
    const cls = s === 'critical' ? 'sev-critical' : s === 'warning' ? 'sev-warning' : 'sev-info';
    return '<span class="sev ' + cls + '">' + esc(t(key)) + '</span>';
  };

  function fmtDateTime(iso, opts) {
    if (!iso) return '—';
    try {
      const d = new Date(iso);
      const loc = settings.get('lang') === 'my' ? 'my-MM' : settings.get('lang') === 'ja' ? 'ja-JP' : 'en-GB';
      return new Intl.DateTimeFormat(loc, Object.assign({ dateStyle: 'medium', timeStyle: 'short' }, opts || {})).format(d);
    } catch (e) { return String(iso); }
  }
  A.fmtDateTime = fmtDateTime;
  A.fmtUTC = function (iso) { return fmtDateTime(iso, { timeZone: 'UTC' }) + ' UTC'; };

  A.fmtTemp = function (c) {
    if (c == null || isNaN(c)) return '—';
    return settings.get('units') === 'imperial' ? Math.round(c * 9 / 5 + 32) + '°F' : Math.round(c) + '°C';
  };
  A.fmtSpeed = function (kmh) {
    if (kmh == null || isNaN(kmh)) return '—';
    return settings.get('units') === 'imperial' ? Math.round(kmh * 0.621371) + ' mph' : Math.round(kmh) + ' km/h';
  };

  function haversineKm(lat1, lon1, lat2, lon2) {
    if ([lat1, lon1, lat2, lon2].some(v => v == null || isNaN(v))) return null;
    const R = 6371, r = Math.PI / 180;
    const dLat = (lat2 - lat1) * r, dLon = (lon2 - lon1) * r;
    const a = Math.sin(dLat / 2) ** 2 + Math.cos(lat1 * r) * Math.cos(lat2 * r) * Math.sin(dLon / 2) ** 2;
    return 2 * R * Math.asin(Math.sqrt(a));
  }
  A.haversineKm = haversineKm;

  // pick first present key (defensive against backend shape drift)
  A.pick = function (obj, keys) {
    if (!obj) return undefined;
    for (const k of keys) if (obj[k] !== undefined && obj[k] !== null) return obj[k];
    return undefined;
  };

  /* ---------- banner ---------- */
  const bannerEl = () => document.getElementById('banner');
  const bannerText = () => document.getElementById('banner-text');
  A.banner = {
    showStale(ageMin) {
      const b = bannerEl(); if (!b) return;
      bannerText().textContent = (ageMin == null)
        ? t('common.unavailable.title') + ' ' + t('banner.stale_suffix')
        : t('common.updated_ago', { min: ageMin }) + ' ' + t('banner.stale_suffix');
      b.hidden = false;
    },
    hideStale() { const b = bannerEl(); if (b) b.hidden = true; }
  };

  /* ---------- safety mode ---------- */
  A.safety = {
    isOn() { return !!settings.get('safetyMode'); },
    apply() {
      document.body.classList.toggle('safety-mode', this.isOn());
      const tg = document.getElementById('safety-toggle');
      if (tg) tg.checked = this.isOn();
      const pinned = document.getElementById('safety-pinned');
      if (pinned) pinned.hidden = !this.isOn();
      if (this.isOn()) A.views.alerts && A.views.alerts.renderPinned && A.views.alerts.renderPinned();
    }
  };

  /* ---------- theme / contrast ---------- */
  function applyChrome() {
    document.body.classList.toggle('dark', settings.get('theme') === 'dark');
    document.body.classList.toggle('hc', !!settings.get('contrast'));
    const tt = document.getElementById('theme-toggle');
    if (tt) { tt.textContent = settings.get('theme') === 'dark' ? '☀️' : '🌙'; tt.setAttribute('aria-pressed', String(settings.get('theme') === 'dark')); }
    const ct = document.getElementById('contrast-toggle');
    if (ct) ct.setAttribute('aria-pressed', String(!!settings.get('contrast')));
  }
  A.applyChrome = applyChrome;

  /* ---------- router: keyboard-navigable tabs ---------- */
  const VIEWS = ['home', 'earthnow', 'map', 'quakes', 'tsunami', 'alerts', 'accuracy', 'settings'];
  A.views = A.views || {};
  let current = 'home';
  function show(view) {
    if (VIEWS.indexOf(view) < 0) view = 'home';
    current = view;
    VIEWS.forEach(v => {
      const sec = document.getElementById('view-' + v);
      const tab = document.querySelector('[data-view="' + v + '"]');
      const on = v === view;
      if (sec) sec.hidden = !on;
      if (tab) { tab.setAttribute('aria-selected', String(on)); tab.tabIndex = on ? 0 : -1; }
    });
    const v = A.views[view];
    if (v && typeof v.render === 'function') v.render(document.getElementById('view-' + view));
    try { window.scrollTo(0, 0); } catch (e) {}
  }
  A.show = show;
  A.refresh = function () { show(current); };

  function bindTabs() {
    const tabs = Array.from(document.querySelectorAll('[role="tab"]'));
    tabs.forEach((tab, i) => {
      tab.addEventListener('click', () => show(tab.dataset.view));
      tab.addEventListener('keydown', e => {
        let j = null;
        if (e.key === 'ArrowRight' || e.key === 'ArrowDown') j = (i + 1) % tabs.length;
        else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') j = (i - 1 + tabs.length) % tabs.length;
        else if (e.key === 'Home') j = 0;
        else if (e.key === 'End') j = tabs.length - 1;
        if (j != null) { e.preventDefault(); tabs[j].focus(); show(tabs[j].dataset.view); }
      });
    });
  }

  /* ---------- onboarding ---------- */
  const OB_KEY = 'aether.onboarded.v1';
  function onboarding() {
    if (loadJson(OB_KEY, false)) return;
    const ov = document.getElementById('onboarding');
    const slides = [1, 2, 3]; let idx = 0;
    const dots = document.getElementById('ob-dots');
    dots.innerHTML = slides.map((_, i) => '<i class="' + (i === 0 ? 'on' : '') + '"></i>').join('');
    function paint() {
      document.getElementById('ob-title').textContent = t('onboarding.s' + slides[idx] + '.title');
      document.getElementById('ob-desc').textContent = t('onboarding.s' + slides[idx] + '.desc');
      document.getElementById('ob-back').hidden = idx === 0;
      document.getElementById('ob-next').textContent = t(idx === slides.length - 1 ? 'onboarding.done' : 'onboarding.next');
      Array.from(dots.children).forEach((d, i) => d.classList.toggle('on', i === idx));
    }
    function done() { saveJson(OB_KEY, true); ov.hidden = true; document.getElementById('ob-next').focus && document.querySelector('[data-view="home"]').focus(); }
    document.getElementById('ob-next').addEventListener('click', () => { idx < slides.length - 1 ? (idx++, paint()) : done(); });
    document.getElementById('ob-back').addEventListener('click', () => { if (idx > 0) { idx--; paint(); } });
    document.getElementById('ob-skip').addEventListener('click', done);
    ov.addEventListener('keydown', e => { if (e.key === 'Escape') done(); });
    ov.hidden = false; paint();
    setTimeout(() => document.getElementById('ob-next').focus(), 50);
  }

  /* ---------- boot ---------- */
  A.boot = async function () {
    await A.i18n.setLang(settings.get('lang') || 'my');
    applyChrome();
    A.safety.apply();
    bindTabs();
    // header controls
    document.getElementById('safety-toggle').addEventListener('change', e => {
      settings.set('safetyMode', e.target.checked); A.safety.apply(); A.refresh();
    });
    document.getElementById('contrast-toggle').addEventListener('click', () => {
      settings.set('contrast', !settings.get('contrast')); applyChrome();
    });
    document.getElementById('theme-toggle').addEventListener('click', () => {
      settings.set('theme', settings.get('theme') === 'dark' ? 'light' : 'dark'); applyChrome();
      if (A.views.map && A.views.map.retheme) A.views.map.retheme();
    });
    document.getElementById('banner-retry').addEventListener('click', () => A.refresh());
    document.addEventListener('click', e => {
      const b = e.target.closest('[data-action="retry"]');
      if (b) A.refresh();
    });
    // re-render views on language/location change
    A.events.on('language', () => { A.i18n.applyStatic(); A.refresh(); });
    A.events.on('location', () => A.refresh());
    A.events.on('units', () => A.refresh());
    onboarding();
    show('home');
    // service worker
    if ('serviceWorker' in navigator) {
      navigator.serviceWorker.register('sw.js').catch(() => {});
    }
  };
})();
