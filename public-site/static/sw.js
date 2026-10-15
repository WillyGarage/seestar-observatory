// Sky app service worker: makes the site installable and keeps the app shell (CSS, icons)
// available offline. Pages, photos and APIs always go to the network so nothing goes stale.
const CACHE = "sky-shell-v1";
const SHELL = ["/static/style.css", "/static/icons/logo.svg", "/static/icons/icon-192.png"];
self.addEventListener("install", e => { e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL))); self.skipWaiting(); });
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))));
  self.clients.claim();
});
self.addEventListener("fetch", e => {
  const u = new URL(e.request.url);
  if (e.request.method !== "GET" || u.origin !== location.origin) return;
  if (SHELL.includes(u.pathname)) {
    // shell: network first so a deploy shows up, cache when offline
    e.respondWith(fetch(e.request).then(r => { const c = r.clone(); caches.open(CACHE).then(x => x.put(e.request, c)); return r; })
      .catch(() => caches.match(e.request)));
  }
});
