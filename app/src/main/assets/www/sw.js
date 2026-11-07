/* AETHER service worker.
   App shell: cache-first (works offline after first visit).
   /v1/* API: network-first with cache fallback (stale data + banner in app).
   Navigations offline: offline.html. */
const SHELL = 'aether-shell-v2';
const DATA = 'aether-data-v1';

const APP_SHELL = [
  './', 'index.html', 'offline.html', 'manifest.json',
  'css/tokens.css', 'css/components.css', 'css/views.css',
  'vendor/leaflet/leaflet.css', 'vendor/leaflet/MarkerCluster.css',
  'vendor/leaflet/MarkerCluster.Default.css',
  'vendor/leaflet/leaflet.js', 'vendor/leaflet/markercluster.js',
  'js/i18n.js', 'js/gazetteer.js', 'js/app.js', 'js/home.js',
  'js/earthnow.js', 'js/mapview.js', 'js/earthquakes.js',
  'js/tsunami.js', 'js/alerts.js', 'js/accuracy.js', 'js/settings.js',
  'i18n/my.json', 'i18n/en.json', 'i18n/ja.json',
  'icons/icon.svg', 'icons/icons.svg', 'icons/icon-192.png', 'icons/icon-512.png'
];

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(SHELL).then(c => c.addAll(APP_SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== SHELL && k !== DATA).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

function isApi(url) {
  return url.pathname.indexOf('/v1/') === 0;
}

self.addEventListener('fetch', event => {
  const { request } = event;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);

  // API: network-first, cache fallback
  if (isApi(url)) {
    event.respondWith(
      fetch(request).then(res => {
        if (res.ok) {
          const copy = res.clone();
          caches.open(DATA).then(c => c.put(request, copy)).catch(() => {});
        }
        return res;
      }).catch(() => caches.open(DATA).then(c => c.match(request)).then(hit => {
        if (hit) return hit;
        return new Response(JSON.stringify({ error: 'offline', provenance: { sources: [], updated_at: null } }), {
          status: 503, headers: { 'Content-Type': 'application/json' }
        });
      }))
    );
    return;
  }

  // Navigations: cache-first, offline fallback page
  if (request.mode === 'navigate') {
    event.respondWith(
      caches.match(request).then(hit => hit || fetch(request).catch(() => caches.match('offline.html')))
    );
    return;
  }

  // Static assets (incl. CDN libs): cache-first, network update in background
  event.respondWith(
    caches.match(request).then(hit => {
      const net = fetch(request).then(res => {
        if (res.ok && (url.origin === self.location.origin || request.url.includes('unpkg.com') || request.url.includes('cdnjs.cloudflare.com'))) {
          const copy = res.clone();
          caches.open(SHELL).then(c => c.put(request, copy)).catch(() => {});
        }
        return res;
      }).catch(() => hit);
      return hit || net;
    })
  );
});
