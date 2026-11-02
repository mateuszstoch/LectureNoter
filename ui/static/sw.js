// Service worker: powłoka aplikacji z cache (działa też, gdy serwer jest wyłączony). API nigdy nie jest cache'owane.
const CACHE = "lecturenoter-v6";
const SHELL = ["/", "/app.js", "/notes.js", "/chat.js", "/app.css", "/fonts/fonts.css", "/vendor/marked.min.js", "/vendor/purify.min.js",
               "/vendor/katex/katex.min.js", "/vendor/katex/katex.min.css", "/manifest.webmanifest", "/favicon.svg",
               "/icons/icon-192.png", "/icons/icon-512.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

// network-first: świeże pliki, gdy serwer działa; z cache, gdy go nie ma
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin || url.pathname.startsWith("/api/")) return;
  e.respondWith(
    fetch(e.request)
      .then((res) => {
        if (res.ok) {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(e.request, copy));
        }
        return res;
      })
      .catch(() => caches.match(e.request).then((r) => r || caches.match("/")))
  );
});
