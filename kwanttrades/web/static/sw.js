// Minimal service worker: keeps ONLY the static app shell so the installed app opens instantly and shows its
// "Reconnecting" state when the server is unreachable. It never touches /api/*, never caches sign-in pages or redirected
// responses, and always prefers the network.
const CACHE = "kwanttrades-shell-v2";
// index.html, the manifest, and everything under css/ js/ (incl. js/screens/) and icons/.
const SHELL = /^\/(?:|index\.html|manifest\.webmanifest|(?:css|js|icons)\/[^?]+)$/;

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin || url.pathname.startsWith("/api/") || url.pathname.startsWith("/login") || !SHELL.test(url.pathname)) return;
  e.respondWith((async () => {
    try {
      const res = await fetch(req);
      if (res.ok && !res.redirected && res.type === "basic" && !res.url.includes("/login")) {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(req, copy)).catch(() => {});
      }
      return res;
    } catch (err) {
      const hit = await caches.match(req, { ignoreSearch: true });
      if (hit) return hit;
      throw err;
    }
  })());
});
