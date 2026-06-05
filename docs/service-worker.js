/* SmartStock PWA service worker.
   Shell = cache-first (offline app). Data = network-first (fresh reports,
   falls back to cache when offline). All paths relative for GitHub Pages
   subpath hosting. Bump CACHE on any shell change. */
'use strict';

const CACHE = 'smartstock-v11';
const SHELL = [
  './',
  'index.html',
  'app.js',
  'style.css',
  'manifest.json',
  'icons/icon-192.png',
  'icons/icon-512.png',
  'icons/apple-touch-icon-180.png',
];

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  // Network-first for EVERYTHING (shell + data), and force {cache:'reload'} so we
  // also bypass the BROWSER HTTP cache (GitHub Pages sets max-age on app.js — the
  // real staleness culprit). Cache refreshed on success, used only offline.
  e.respondWith(
    fetch(new Request(req.url, { cache: 'reload' })).then((res) => {
      const copy = res.clone();
      caches.open(CACHE).then((c) => c.put(req, copy));
      return res;
    }).catch(() => caches.match(req))
  );
});
