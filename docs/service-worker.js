/* SmartStock PWA service worker.
   Shell = cache-first (instant, offline-capable); Data = network-first + timeout (fresh reports,
   fast cache fallback on a stalled/captive network). All paths relative for GitHub Pages subpath
   hosting. Bump CACHE on any shell change — a new CACHE name triggers a fresh install that
   re-fetches the shell with {cache:'reload'} (bypassing the GitHub Pages HTTP max-age, the real
   staleness culprit), so a deploy reliably propagates.
   NOTE: deliberately NO controllerchange->location.reload() handler — combined with skipWaiting it
   loops forever into a blank screen. Updates ride the CACHE-version bump + cache-first serving. */
'use strict';

const CACHE = 'smartstock-v57';   // C1 schema_version client guard（與 app.js APP_VERSION 同步 bump）
const DATA_TIMEOUT_MS = 3500;     // stalled-network cutoff before falling back to cached data
const SHELL = [
  './',
  'index.html',
  'app.js',
  'style.css',
  'manifest.json',
  'vendor/lightweight-charts.standalone.production.js',
  'vendor/gsap.min.js',
  'vendor/gsap-flip.min.js',
  'vendor/fonts/ss-numerals.woff2',
  'icons/icon-192.png',
  'icons/icon-512.png',
  'icons/apple-touch-icon-180.png',
];

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE).then((c) =>
      // per-asset add (NOT addAll) so a single 404/renamed path can't reject the whole install and
      // leave the SW stuck un-activated; {cache:'reload'} fetches the FRESH shell past the HTTP
      // cache so a version bump actually picks up the new app.js/css.
      Promise.all(SHELL.map((u) =>
        c.add(new Request(u, { cache: 'reload' })).catch(() => undefined)))
    ).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

// cache a fresh response — clone SYNCHRONOUSLY here, before the response body is handed to the page
// and consumed (cloning AFTER the page reads it throws 'body already used' and silently skips the
// cache — exactly why the data cache was empty in testing).
function _cachePut(req, res) {
  if (res && res.ok && res.type !== 'opaque') {
    const copy = res.clone();
    caches.open(CACHE).then((c) => c.put(req, copy)).catch(() => undefined);
  }
}

// A navigation request must NEVER resolve to Response.error() — that surfaces the browser's native
// blank 'cannot open page'. Fall back to any cached shell document, else a tiny inline offline page.
function _navFallback(req) {
  if (req && req.mode === 'navigate') {
    return caches.match('index.html')
      .then((i) => i || caches.match('./'))
      .then((s) => s || new Response(
        '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">' +
        '<title>SmartStock</title><body style="font-family:system-ui;padding:2rem;color:#333">' +
        '離線中，且首頁尚未快取。請連線後重新開啟。</body>',
        { status: 200, headers: { 'Content-Type': 'text/html; charset=utf-8' } }));
  }
  return Response.error();
}

// DATA (daily reports / verdicts / detail / universe): network-first so the freshest report shows,
// but RACE a timeout — a stalled or captive-portal network falls back to cached data fast instead
// of hanging the open. Fresh copy is cached for offline.
function networkFirstData(req) {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (r) => { if (!settled && r) { settled = true; resolve(r); } };
    const timer = setTimeout(() => caches.match(req).then(finish), DATA_TIMEOUT_MS);
    fetch(new Request(req.url, { cache: 'reload' })).then((res) => {
      clearTimeout(timer);
      _cachePut(req, res);
      if (!settled) { settled = true; resolve(res); }
    }).catch(() => {
      clearTimeout(timer);
      caches.match(req).then((c) => { if (!settled) { settled = true; resolve(c || _navFallback(req)); } });
    });
  });
}

// SHELL: cache-first for instant opens; revalidate in the background (also past the HTTP cache) so
// the cached copy stays warm between version bumps. Cache miss → network → (offline) nav fallback.
function cacheFirstShell(req) {
  return caches.match(req).then((cached) => {
    const fresh = fetch(new Request(req.url, { cache: 'reload' })).then((res) => {
      _cachePut(req, res);
      return res;
    }).catch(() => null);
    if (cached) return cached;
    return fresh.then((res) => res || _navFallback(req));
  });
}

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  let isData = false;
  try { isData = new URL(req.url).pathname.includes('/data/'); } catch (err) { isData = false; }
  e.respondWith(isData ? networkFirstData(req) : cacheFirstShell(req));
});
