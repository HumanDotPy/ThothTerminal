(function () {
  function formEncode(fields) {
    const params = new URLSearchParams();
    Object.entries(fields).forEach(([k, v]) => params.set(k, v == null ? "" : String(v)));
    return params;
  }

  function presenceHeaders(extra) {
    if (window.ThothClientPresence && typeof window.ThothClientPresence.headers === "function") {
      return window.ThothClientPresence.headers(extra || {});
    }
    return Object.assign({}, extra || {});
  }

  function postJson(url, data) {
    return fetch(url, {
      method: "POST",
      headers: presenceHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(data || {}),
    });
  }

  function postForm(url, data) {
    return fetch(url, {
      method: "POST",
      headers: presenceHeaders({ "Content-Type": "application/x-www-form-urlencoded" }),
      body: formEncode(data || {}),
    });
  }

  const THOTH_CONFIG = window.THOTH_CONFIG || {};

  function setupDocBackButton() {
    const btn = document.getElementById("doc-back-btn");
    if (!btn) return;
    if (btn.dataset.docBackBound === "1") return;
    btn.dataset.docBackBound = "1";
    btn.addEventListener("click", (e) => {
      if (e) e.preventDefault();
      const href = btn.getAttribute("href") || "/";
      window.location.href = href;
    });
  }

  function markDocumentOpened() {
    const panel = document.getElementById("doc-panel");
    if (!panel) return;
    const docId = panel.getAttribute("data-doc-id");
    const opened = panel.getAttribute("data-opened");
    if (!docId || opened === "1" || panel.dataset.openRequestSent === "1") return;
    panel.dataset.openRequestSent = "1";
    postForm(`/documents/${encodeURIComponent(docId)}/open`, {})
      .then((res) => {
        if (res && res.ok) panel.setAttribute("data-opened", "1");
      })
      .catch(() => {
        panel.dataset.openRequestSent = "";
      });
  }

  function initDocDetail() {
    setupDocBackButton();
    markDocumentOpened();
    if (window.ThothDocReader && typeof window.ThothDocReader.init === "function") {
      window.ThothDocReader.init();
    }
  }

  // Seen tracking (strict gating)
  (function () {
    if (!THOTH_CONFIG.enableSeen) return;
    const docs = Array.from(document.querySelectorAll(".doc[data-doc-id]"));
    if (docs.length === 0) return;

    const dwellMs = Number(THOTH_CONFIG.seenDwellMs || 2000);
    const scrollWindowMs = Number(THOTH_CONFIG.seenScrollWindowS || 10) * 1000;
    const afkMs = Number(THOTH_CONFIG.seenAfkS || 30) * 1000;

    let lastInputAt = Date.now();
    let lastScrollAt = 0;

    function markInput() {
      lastInputAt = Date.now();
    }

    window.addEventListener("mousemove", markInput, { passive: true });
    window.addEventListener("mousedown", markInput, { passive: true });
    window.addEventListener("keydown", markInput, { passive: true });
    window.addEventListener("scroll", () => {
      lastScrollAt = Date.now();
    }, { passive: true });

    function shouldMarkSeen() {
      if (!document.hasFocus()) return false;
      const now = Date.now();
      if (now - lastInputAt > afkMs) return false;
      const lastNavRef = window.__THOTH_LAST_NAV;
      const lastNavAt = lastNavRef && lastNavRef.ts ? lastNavRef.ts : 0;
      const recentScrollOrNav = (now - Math.max(lastScrollAt, lastNavAt)) <= scrollWindowMs;
      if (!recentScrollOrNav) return false;
      return true;
    }

    const seenQueue = new Set();
    let seenFlushTimer = null;
    let seenFlushing = false;

    function scheduleSeenFlush() {
      if (seenFlushTimer) return;
      seenFlushTimer = setTimeout(() => {
        flushSeen();
      }, 700);
    }

    async function flushSeen() {
      if (seenFlushing) return;
      seenFlushing = true;
      if (seenFlushTimer) {
        clearTimeout(seenFlushTimer);
        seenFlushTimer = null;
      }
      if (seenQueue.size === 0) {
        seenFlushing = false;
        return;
      }
      const ids = Array.from(seenQueue);
      try {
        const res = await postJson("/documents/bulk", { doc_ids: ids, action: "seen" });
        if (res.ok) {
          let failures = new Set();
          try {
            const json = await res.json();
            const rawFailures = Array.isArray(json.failures) ? json.failures : [];
            failures = new Set(rawFailures.map((v) => Number(v)));
          } catch (_e) {}

          ids.forEach((id) => {
            if (failures.has(Number(id))) return;
            seenQueue.delete(id);
            const el = document.querySelector(`.doc[data-doc-id="${id}"]`);
            if (el) el.dataset.seenSent = "1";
          });
        }
      } catch (_e) {}

      seenFlushing = false;
      if (seenQueue.size > 0) scheduleSeenFlush();
    }

    function enqueueSeen(docId, el) {
      if (!docId) return;
      if (el && el.dataset.seenQueued === "1") return;
      seenQueue.add(docId);
      if (el) el.dataset.seenQueued = "1";
      scheduleSeenFlush();
    }

    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          const el = entry.target;
          if (!entry.isIntersecting || entry.intersectionRatio < 0.6) {
            if (el.__seenTimer) {
              clearTimeout(el.__seenTimer);
              el.__seenTimer = null;
            }
            return;
          }
          if (el.dataset.seenSent === "1") return;
          if (el.__seenTimer) return;
          el.__seenTimer = setTimeout(async () => {
            el.__seenTimer = null;
            if (el.dataset.seenSent === "1") return;
            if (!shouldMarkSeen()) return;
            const docId = el.getAttribute("data-doc-id");
            if (!docId) return;
            enqueueSeen(docId, el);
          }, dwellMs);
        });
      },
      { threshold: [0.6] }
    );

    docs.forEach((d) => observer.observe(d));
  })();

  // Doc triage actions + keyboard navigation
  (function () {
    const docEls = Array.from(document.querySelectorAll(".doc[data-doc-id]"));
    const docDetailButtons = Array.from(document.querySelectorAll("button.doc-triage[data-doc-action]"));
    if (docEls.length === 0 && docDetailButtons.length === 0) return;

    function isFeedRoute() {
      return !!(document.body && document.body.classList.contains("route-feed"));
    }

    function currentDocId() {
      const selected = document.querySelector(".doc.selected") || docEls[0];
      return selected ? selected.getAttribute("data-doc-id") : null;
    }

    async function applyAction(docId, action) {
      if (!docId || !action) return;
      let act = action;
      const docEl = document.querySelector(`.doc[data-doc-id="${docId}"]`);
      if (action === "save" && docEl && docEl.getAttribute("data-saved") === "1") {
        act = "unsave";
      }
      try {
        await postForm(`/documents/${encodeURIComponent(docId)}/${act}`, {});
        window.location.reload();
      } catch (_e) {}
    }

    document.addEventListener("click", (e) => {
      const btn = e.target.closest("button.doc-triage[data-doc-action]");
      if (!btn) return;
      const action = (btn.getAttribute("data-doc-action") || "").toLowerCase();
      const docId = btn.getAttribute("data-doc-id") || btn.closest(".doc")?.getAttribute("data-doc-id");
      if (!docId) return;
      applyAction(docId, action);
    });

    if (docEls.length > 0) {
      let idx = 0;
      const lastNavRef = { ts: 0 };
      function setSelected(i, opts) {
        docEls.forEach((d) => d.classList.remove("selected"));
        const safe = Math.max(0, Math.min(i, docEls.length - 1));
        idx = safe;
        const el = docEls[idx];
        if (el) {
          el.classList.add("selected");
          if (opts && opts.scroll) {
            el.scrollIntoView({ block: "nearest" });
          }
        }
      }
      setSelected(0, { scroll: false });

      document.addEventListener("keydown", (e) => {
        if (window.__THOTH_OVERLAY_OPEN) return;
        if (!isFeedRoute()) return;
        const tag = (e.target && e.target.tagName || "").toLowerCase();
        if (["input", "textarea", "select"].includes(tag)) return;

        if (e.key === "j") {
          lastNavRef.ts = Date.now();
          setSelected(idx + 1, { scroll: true });
          e.preventDefault();
        } else if (e.key === "k") {
          lastNavRef.ts = Date.now();
          setSelected(idx - 1, { scroll: true });
          e.preventDefault();
        } else if (e.key === "Enter") {
          const el = docEls[idx];
          const link = el ? el.querySelector("h3 a") : null;
          if (link && link.getAttribute("href")) {
            window.location.href = link.getAttribute("href");
            e.preventDefault();
          }
        } else if (e.key === "s") {
          const id = currentDocId();
          if (id) applyAction(id, "save");
          e.preventDefault();
        } else if (e.key === "x") {
          const id = currentDocId();
          if (id) applyAction(id, "ignore");
          e.preventDefault();
        } else if (e.key === "a") {
          const id = currentDocId();
          if (id) applyAction(id, "actioned");
          e.preventDefault();
        } else if (e.key === "u") {
          const id = currentDocId();
          if (id) applyAction(id, "reset_to_new");
          e.preventDefault();
        } else if (e.key === "/") {
          const input = document.getElementById("feed-search-input") || document.querySelector('input[name="q"]');
          if (input) {
            input.focus();
            e.preventDefault();
          }
        }
      });

      // Expose nav timestamp for seen tracking.
      window.__THOTH_LAST_NAV = lastNavRef;
    }
  })();

  window.ThothDocumentInteractions = Object.assign(window.ThothDocumentInteractions || {}, {
    initDocDetail,
    markDocumentOpened,
  });

  initDocDetail();
})();
