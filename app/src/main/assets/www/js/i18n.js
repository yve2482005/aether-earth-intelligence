/* AETHER i18n — flat keys, default language "my" (Burmese-first).
   Official warning categories are NEVER translated in meaning:
   keys tsunami.cat.* keep the English original; .desc keys carry the
   localized explanation shown side by side. */
(function () {
  'use strict';
  const A = (window.AETHER = window.AETHER || {});
  const FALLBACKS = { my: ['my', 'en'], en: ['en', 'my'], ja: ['ja', 'en', 'my'] };
  const cache = {};

  async function loadDict(lang) {
    if (cache[lang]) return cache[lang];
    const res = await fetch('i18n/' + lang + '.json', { cache: 'force-cache' });
    if (!res.ok) throw new Error('i18n ' + lang + ' HTTP ' + res.status);
    const dict = await res.json();
    cache[lang] = dict;
    return dict;
  }

  const i18n = {
    lang: 'my',
    dicts: {},
    async setLang(lang) {
      const chain = FALLBACKS[lang] || FALLBACKS.my;
      this.dicts = {};
      for (const l of chain) {
        try { this.dicts[l] = await loadDict(l); } catch (e) { /* skip */ }
      }
      this.lang = lang;
      document.documentElement.lang = lang;
      this.applyStatic();
    },
    t(key, vars) {
      const chain = [this.lang].concat(FALLBACKS[this.lang] || []).filter((v, i, a) => a.indexOf(v) === i);
      let s;
      for (const l of chain) {
        const d = this.dicts[l];
        if (d && Object.prototype.hasOwnProperty.call(d, key)) { s = d[key]; break; }
      }
      if (typeof s !== 'string') return key; // honest: show key, never fabricate
      if (vars) for (const k of Object.keys(vars)) s = s.split('{' + k + '}').join(String(vars[k]));
      return s;
    },
    applyStatic(root) {
      const scope = root || document;
      scope.querySelectorAll('[data-i18n]').forEach(el => {
        el.textContent = this.t(el.getAttribute('data-i18n'));
      });
      scope.querySelectorAll('[data-i18n-aria]').forEach(el => {
        el.setAttribute('aria-label', this.t(el.getAttribute('data-i18n-aria')));
      });
      scope.querySelectorAll('[data-i18n-title]').forEach(el => {
        el.setAttribute('title', this.t(el.getAttribute('data-i18n-title')));
      });
      scope.querySelectorAll('[data-i18n-ph]').forEach(el => {
        el.setAttribute('placeholder', this.t(el.getAttribute('data-i18n-ph')));
      });
    }
  };
  A.i18n = i18n;
  A.t = (k, v) => i18n.t(k, v);
})();
