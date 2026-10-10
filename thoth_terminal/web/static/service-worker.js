const CACHE_NAME = "thoth-shell-v23";
const SHELL_ASSETS = [
  "/manifest.json",
  "/static/offline.html",
];
const RUNTIME_CACHE_PATHS = new Set([
  "/manifest.json",
  "/static/offline.html",
  "/static/app.css",
  "/static/app.js",
  "/static/agents_workspace.js",
  "/static/terminal-redesign.css",
  "/static/terminal-home.css",
  "/static/terminal-feed.css",
  "/static/terminal-agents.css",
  "/static/charting.js",
  "/static/capture_url_check.js",
  "/static/doc_reader.js",
  "/static/document_interactions.js",
  "/static/desktop_shell.js",
  "/static/entity_search_widgets.js",
  "/static/feed_interactions.js",
  "/static/form_draft_autosave.js",
  "/static/list_view_helpers.js",
  "/static/media_lightbox.js",
  "/static/note_editor.js",
  "/static/sparkline_charts.js",
  "/static/thesis_autosave.js",
  "/static/thesis_table_entry.js",
  "/static/visualizations.js",
  "/static/icons/thoth-terminal.ico",
  "/static/icons/thoth-terminal-chatgpt-128.png",
]);

function shouldRuntimeCache(url) {
  return RUNTIME_CACHE_PATHS.has(url.pathname);
}

async function pruneOldVersions(cache, url) {
  const keys = await cache.keys();
  await Promise.all(
    keys
      .filter((key) => {
        const cached = new URL(key.url);
        return cached.origin === url.origin && cached.pathname === url.pathname && cached.href !== url.href;
      })
      .map((key) => cache.delete(key))
  );
}

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE_NAME)
      .then((cache) => cache.addAll(SHELL_ASSETS))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key)))
      )
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET") return;
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/documents/") || url.pathname.startsWith("/mcp")) {
    return;
  }

  if (event.request.mode === "navigate") {
    event.respondWith(
      fetch(event.request).catch(() => caches.match("/static/offline.html"))
    );
    return;
  }

  if (!shouldRuntimeCache(url)) {
    event.respondWith(fetch(event.request));
    return;
  }

  event.respondWith(
    caches.match(event.request).then((cached) => {
      if (cached) return cached;
      return fetch(event.request).then((resp) => {
        if (resp && resp.ok) {
          const copy = resp.clone();
          caches.open(CACHE_NAME).then(async (cache) => {
            await pruneOldVersions(cache, url);
            await cache.put(event.request, copy);
          });
        }
        return resp;
      });
    })
  );
});
