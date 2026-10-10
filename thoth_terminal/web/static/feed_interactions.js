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

  function postJson(url, data, signal) {
    return fetch(url, {
      method: "POST",
      headers: presenceHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(data || {}),
      signal: signal || AbortSignal.timeout(60000),
    });
  }

  function postForm(url, data) {
    return fetch(url, {
      method: "POST",
      headers: presenceHeaders({ "Content-Type": "application/x-www-form-urlencoded" }),
      body: formEncode(data || {}),
      signal: AbortSignal.timeout(60000),
    });
  }

  function isAbortError(error) {
    return error && error.name === "AbortError";
  }

  function quickToast(message) {
    if (typeof window.quickToast === "function") {
      window.quickToast(message);
    }
  }

  async function responseJson(response) {
    const payload = await response.json().catch(() => ({}));
    if (!response.ok || payload.ok === false) {
      const error = new Error(payload.error || payload.detail || `Request failed (HTTP ${response.status}).`);
      error.payload = payload;
      error.httpStatus = response.status;
      throw error;
    }
    return payload;
  }

  const feedSelection = { row: null, payload: null };

  function safeHref(value) {
    try {
      const url = new URL(value, window.location.href);
      return ["http:", "https:"].includes(url.protocol) ? url.href : "";
    } catch (_e) { return ""; }
  }

  function updateDocState(doc) {
    const row = document.querySelector(`.feed-signal-card[data-doc-id="${Number(doc.id)}"]`);
    if (!row) return;
    row.dataset.triage = doc.triage_state || row.dataset.triage;
    if (typeof doc.saved === "boolean") row.dataset.saved = doc.saved ? "1" : "0";
    const state = row.querySelector(".feed-triage-state");
    if (state) state.textContent = row.dataset.triage;
    row.querySelectorAll('[data-doc-action="save"]').forEach((button) => { button.textContent = row.dataset.saved === "1" ? "Saved" : "Save"; });
    if (feedSelection.row === row) {
      document.querySelectorAll('[data-inspector-state], [data-inspector-triage]').forEach((node) => { node.textContent = row.dataset.triage; });
      const saved = document.querySelector('[data-inspector-saved]');
      if (saved) saved.textContent = row.dataset.saved === "1" ? "Yes" : "No";
      const save = document.querySelector('[data-inspector-action="save"]');
      if (save) { save.textContent = row.dataset.saved === "1" ? "Unsave" : "Save"; save.setAttribute("aria-pressed", row.dataset.saved === "1" ? "true" : "false"); }
    }
  }

  function setupEarningsRailToggle() {
    const toggle = document.querySelector("[data-earnings-rail-toggle]");
    if (!toggle) return;
    toggle.addEventListener("change", () => {
      const val = toggle.checked ? "1" : "0";
      document.cookie = `thoth_earnings_rail=${val}; Path=/; Max-Age=31536000`;
      window.location.reload();
    });
  }

  function setupEvidenceModal() {
    const overlay = document.getElementById("evidence-overlay");
    if (!overlay) return;
    const closeBtn = document.getElementById("evidence-close");
    const submitBtn = document.getElementById("evidence-submit");
    const searchInput = document.getElementById("evidence-thesis-search");
    const results = document.getElementById("evidence-thesis-results");
    const thesisIdEl = document.getElementById("evidence-thesis-id");
    const polarityEl = document.getElementById("evidence-polarity");
    const snippetEl = document.getElementById("evidence-snippet");
    const noteEl = document.getElementById("evidence-note");
    const docPill = document.getElementById("evidence-doc-pill");
    let activeDocId = null;
    let focusReturn = null;
    const status = document.getElementById("evidence-status");
    let stopSearch = () => {};

    function openOverlay(docId, title) {
      activeDocId = docId;
      if (docPill) docPill.textContent = title ? `doc · ${title}` : `doc #${docId}`;
      overlay.style.display = "flex";
      window.__THOTH_OVERLAY_OPEN = true;
      focusReturn = document.activeElement;
      if (status) status.textContent = "";
      if (searchInput) searchInput.focus();
    }

    function closeOverlay() {
      if (submitBtn?.disabled) return;
      stopSearch();
      overlay.style.display = "none";
      window.__THOTH_OVERLAY_OPEN = false;
      focusReturn?.focus();
      activeDocId = null;
      if (searchInput) searchInput.value = "";
      if (results) results.innerHTML = "";
      if (thesisIdEl) thesisIdEl.value = "";
      if (snippetEl) snippetEl.value = "";
      if (noteEl) noteEl.value = "";
    }
    overlay.addEventListener("keydown", (event) => {
      if (event.key === "Escape") closeOverlay();
      if (event.key !== "Tab") return;
      const nodes = Array.from(overlay.querySelectorAll('button, input, select, textarea, a[href]')).filter((node) => !node.disabled && node.offsetParent !== null);
      if (event.shiftKey && document.activeElement === nodes[0]) { event.preventDefault(); nodes[nodes.length - 1]?.focus(); }
      else if (!event.shiftKey && document.activeElement === nodes[nodes.length - 1]) { event.preventDefault(); nodes[0]?.focus(); }
    });

    document.addEventListener("click", (e) => {
      const btn = e.target && e.target.closest ? e.target.closest(".doc-action[data-action='evidence']") : null;
      if (!btn) return;
      const docId = btn.getAttribute("data-doc-id");
      const card = btn.closest(".doc") || document.querySelector(`.feed-signal-card[data-doc-id="${Number(docId)}"]`);
      const titleEl = card ? card.querySelector("h3") : null;
      const title = titleEl ? titleEl.textContent.trim() : "";
      if (docId) openOverlay(docId, title);
    });

    if (closeBtn) closeBtn.addEventListener("click", closeOverlay);
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) closeOverlay();
    });

    if (searchInput && results && thesisIdEl) {
      let timer = null;
      let qid = 0;
      let searchAbort = null;

      function abortSearch() {
        if (searchAbort) {
          searchAbort.abort();
          searchAbort = null;
        }
      }
      stopSearch = () => { clearTimeout(timer); ++qid; abortSearch(); };
      window.addEventListener("pagehide", stopSearch);

      searchInput.addEventListener("input", () => {
        clearTimeout(timer);
        ++qid;
        abortSearch();
        const q = (searchInput.value || "").trim();
        thesisIdEl.value = "";
        results.innerHTML = "";
        results.style.display = "none";
        if (!q) {
          abortSearch();
          return;
        }
        timer = setTimeout(async () => {
          const myId = ++qid;
          abortSearch();
          const controller = new AbortController();
          searchAbort = controller;
          const deadline = setTimeout(() => controller.abort(), 45000);
          try {
            const res = await fetch(`/api/search?kind=thesis&q=${encodeURIComponent(q)}&limit=12`, {
              headers: presenceHeaders({ Accept: "application/json" }),
              signal: controller.signal,
            });
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const json = await res.json();
            if (myId !== qid) return;
            const items = Array.isArray(json.items) ? json.items : [];
            results.innerHTML = "";
            items.forEach((it) => {
              const row = document.createElement("button");
              row.type = "button";
              row.className = "search-item";
              row.textContent = it.label || String(it.id);
              row.addEventListener("click", () => {
                thesisIdEl.value = String(it.id);
                searchInput.value = it.label || String(it.id);
                results.innerHTML = "";
                results.style.display = "none";
              });
              results.appendChild(row);
            });
            results.style.display = items.length ? "block" : "none";
          } catch (error) {
            if (isAbortError(error)) return;
            results.innerHTML = "";
            results.style.display = "none";
            if (status) status.textContent = "Could not search theses. Try again.";
          } finally {
            clearTimeout(deadline);
            if (searchAbort === controller) searchAbort = null;
          }
        }, 150);
      });
    }

    if (submitBtn) {
      submitBtn.addEventListener("click", async () => {
        if (!activeDocId || submitBtn.disabled) return;
        const thesisId = thesisIdEl ? thesisIdEl.value : "";
        if (!thesisId) {
          if (status) status.textContent = "Select a thesis first.";
          return;
        }
        const payload = {
          document_id: Number(activeDocId),
          polarity: polarityEl ? polarityEl.value : "support",
          snippet: snippetEl ? snippetEl.value : "",
          note: noteEl ? noteEl.value : "",
        };
        const submittedDocId = activeDocId;
        submitBtn.disabled = true;
        try {
          const res = await postJson(`/api/theses/${encodeURIComponent(thesisId)}/evidence`, payload);
          const receipt = await responseJson(res);
          quickToast(receipt.created ? "Evidence attached." : "This source is already linked to that thesis.");
          window.dispatchEvent(new CustomEvent("thoth:feed-research-saved", { detail: { docId: submittedDocId } }));
          submitBtn.disabled = false;
          closeOverlay();
        } catch (error) { if (status) status.textContent = error.message; }
        finally { submitBtn.disabled = false; }
      });
    }
  }

  function setupBulkActions() {
    const forms = Array.from(document.querySelectorAll("[data-action-form]"));
    const bulkBar = document.getElementById("bulk-bar");
    const bulkCount = document.getElementById("bulk-count");
    const bulkClear = document.querySelector("[data-bulk-clear]");

    function checkboxes() {
      return Array.from(document.querySelectorAll('input[type="checkbox"][data-doc-id]'));
    }

    function selectedIds() {
      return checkboxes().filter((c) => c.checked).map((c) => c.getAttribute("data-doc-id"));
    }

    function sync() {
      const ids = selectedIds();
      const joined = ids.join(",");
      forms.forEach((f) => {
        const idsInput = f.querySelector('input[name="ids"]');
        if (idsInput) idsInput.value = joined;
      });
      const has = ids.length > 0;
      document.querySelectorAll("[data-requires-selection]").forEach((btn) => {
        btn.disabled = !has;
        btn.classList.toggle("disabled", !has);
      });
      if (bulkBar) bulkBar.style.display = has ? "flex" : "none";
      if (bulkCount) bulkCount.textContent = `${ids.length} selected`;
      document.querySelectorAll(".doc[data-doc-id]").forEach((card) => {
        const id = card.getAttribute("data-doc-id");
        card.classList.toggle("bulk-selected", ids.includes(id));
      });
    }

    document.addEventListener("change", (e) => {
      const target = e.target;
      if (target && target.matches && target.matches('input[type="checkbox"][data-doc-id]')) sync();
    });
    sync();

    forms.forEach((f) => {
      f.addEventListener("submit", (e) => {
        const ids = selectedIds();
        if (ids.length === 0) {
          e.preventDefault();
          return;
        }
        const idsInput = f.querySelector('input[name="ids"]');
        if (idsInput) idsInput.value = ids.join(",");
      });
    });

    document.addEventListener("click", async (e) => {
      const btn = e.target && e.target.closest ? e.target.closest("[data-bulk-action]") : null;
      if (!btn) return;
      const action = btn.getAttribute("data-bulk-action");
      const ids = selectedIds();
      if (!action || ids.length === 0) return;
      document.querySelectorAll("[data-bulk-action]").forEach((node) => { node.disabled = true; });
      try {
        const response = await postJson("/documents/bulk", { doc_ids: ids, action });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || `Bulk action failed (HTTP ${response.status}).`);
        const failures = new Set((payload.failures || []).map(String));
        ids.filter((id) => !failures.has(id)).forEach((id) => {
          const update = { id };
          if (action === "save" || action === "unsave") update.saved = action === "save";
          else update.triage_state = { actioned: "ACTIONED", ignore: "IGNORED", archive: "ARCHIVED" }[action];
          updateDocState(update);
          const cb = checkboxes().find((node) => node.dataset.docId === id); if (cb) cb.checked = false;
        });
        quickToast(failures.size ? `${failures.size} items could not be updated; they remain selected.` : `${ids.length} items updated.`);
      } catch (error) { quickToast(error.message); }
      finally { sync(); }
    });

    if (bulkClear) {
      bulkClear.addEventListener("click", () => {
        checkboxes().forEach((cb) => {
          cb.checked = false;
        });
        sync();
      });
    }
  }

  function setupFeedDocActions() {
    const dialog = document.getElementById("feed-research-dialog");
    if (!dialog) return;
    const form = dialog.querySelector("form");
    const status = dialog.querySelector("[data-research-status]");
    const similar = dialog.querySelector("[data-research-similar]");
    const forceWrap = dialog.querySelector("[data-research-force-wrap]");
    const submit = dialog.querySelector("[data-research-submit]");
    const result = dialog.querySelector("[data-research-result]");
    let context = null;
    let focusReturn = null;

    function close() {
      if (submit.disabled && !context?.saved) return;
      dialog.close();
      window.__THOTH_OVERLAY_OPEN = false;
      focusReturn?.focus();
    }
    dialog.querySelector("[data-research-close]").addEventListener("click", close);
    dialog.addEventListener("cancel", (event) => { event.preventDefault(); close(); });
    document.addEventListener("click", (event) => {
      const button = event.target.closest("button.doc-action[data-action]");
      if (!button || !["note", "thesis", "gap"].includes(button.dataset.action)) return;
      const row = button.closest(".feed-signal-card") || document.querySelector(`.feed-signal-card[data-doc-id="${Number(button.dataset.docId)}"]`);
      if (!row) return;
      const kind = button.dataset.action;
      const title = row.querySelector("h3")?.textContent.trim() || `Document ${row.dataset.docId}`;
      context = { kind, docId: row.dataset.docId, sourceUrl: row.dataset.sourceUrl, title, requestId: crypto.randomUUID(), saved: false };
      focusReturn = button;
      form.reset();
      submit.disabled = false;
      result.hidden = true;
      forceWrap.hidden = true;
      similar.replaceChildren();
      status.textContent = "";
      dialog.querySelector("#feed-research-heading").textContent = { note: "Add linked note", thesis: "Seed thesis", gap: "Create research gap" }[kind];
      dialog.querySelector("[data-research-source]").textContent = `Source: ${title} · document #${context.docId}`;
      form.elements.title.value = kind === "gap" ? `Research gap: ${title}` : title;
      dialog.querySelector("[data-research-body-label]").textContent = { note: "Your interpretation / research note", thesis: "Thesis statement", gap: "Missing evidence and desired output" }[kind];
      dialog.querySelector("[data-research-entity-hint]").textContent = kind === "thesis" ? "(required, comma separated)" : "(optional, comma separated)";
      form.elements.entities.required = kind === "thesis";
      form.elements.entities.value = feedSelection.row === row ? (feedSelection.payload?.entities || []).join(", ") : "";
      submit.textContent = { note: "Save linked note", thesis: "Create thesis", gap: "Create gap" }[kind];
      dialog.showModal();
      window.__THOTH_OVERLAY_OPEN = true;
      form.elements.body.focus();
    });

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!context || context.saved || submit.disabled) return;
      submit.disabled = true;
      status.textContent = "Saving…";
      const title = form.elements.title.value.trim();
      const body = form.elements.body.value.trim();
      const entities = form.elements.entities.value.trim();
      if (!title || !body) { status.textContent = "Add a title and research text."; submit.disabled = false; return; }
      try {
        const payload = context.kind === "gap" ? {
          title, summary: body, intent: "research_gap", status: "draft", source_kind: "feed", automation_source: "human:feed",
          target_entities: entities.split(",").map((value) => value.trim()).filter(Boolean),
          evidence_bundle: [{ document_id: Number(context.docId), title: context.title, url: context.sourceUrl }],
          linked_objects: { document_ids: [Number(context.docId)] }, provenance_summary: `Created from Feed document #${context.docId}`,
        } : { kind: context.kind, title, body, entities, request_id: context.requestId, force_create: form.elements.force_create.checked };
        const url = context.kind === "gap" ? "/api/instrument-requests" : `/api/feed/documents/${context.docId}/research`;
        const receipt = await responseJson(await postJson(url, payload));
        context.saved = true;
        const href = safeHref(receipt.href || receipt.redirect);
        if (!href) throw new Error("Saved, but the server did not return a research link. Check the research list before retrying.");
        status.textContent = `${context.kind === "gap" ? "Research gap" : context.kind === "note" ? "Linked note" : "Thesis"} #${receipt.id || receipt.item?.id} saved.`;
        result.href = href;
        result.hidden = false;
        forceWrap.hidden = true;
        similar.replaceChildren();
        if (receipt.doc) updateDocState(receipt.doc);
        window.dispatchEvent(new CustomEvent("thoth:feed-research-saved", { detail: { docId: context.docId } }));
      } catch (error) {
        status.textContent = error.message;
        if (context.kind === "gap" && (!error.httpStatus || error.httpStatus >= 500)) {
          context.saved = true;
          status.textContent = "The save outcome is uncertain. Check research requests before creating this gap again.";
          result.href = "/instruments/requests"; result.hidden = false;
        }
        similar.replaceChildren();
        (error.payload?.similar || []).forEach((item) => {
          const link = document.createElement("a"); link.href = safeHref(item.href); link.textContent = item.title; link.target = "_blank"; link.rel = "noopener noreferrer"; similar.appendChild(link);
        });
        forceWrap.hidden = !(error.payload?.similar || []).length;
        submit.disabled = context.saved;
      }
    });
  }

  function setupFeedSavedViews() {
    const containers = Array.from(document.querySelectorAll("[data-saved-views]"));
    if (containers.length === 0) return;

    function keyFor(scope) {
      return `thoth_saved_views_${scope}`;
    }

    function read(scope) {
      try {
        const raw = localStorage.getItem(keyFor(scope));
        return raw ? JSON.parse(raw) : [];
      } catch (_e) {
        return [];
      }
    }

    function write(scope, views) {
      try {
        localStorage.setItem(keyFor(scope), JSON.stringify(views.slice(0, 20)));
      } catch (_e) {}
    }

    containers.forEach((container) => {
      const scope = container.getAttribute("data-saved-views") || "feed";
      const nameInput = container.querySelector("[data-saved-views-name]");
      const saveBtn = container.querySelector("[data-saved-views-save]");
      const list = container.querySelector("[data-saved-views-list]");
      if (!list || !saveBtn) return;

      function render() {
        const views = read(scope);
        list.innerHTML = "";
        if (!views.length) {
          const empty = document.createElement("span");
          empty.className = "pill";
          empty.textContent = "No saved views yet";
          list.appendChild(empty);
          return;
        }
        views.forEach((view, idx) => {
          const wrap = document.createElement("div");
          wrap.style.display = "flex";
          wrap.style.gap = "6px";
          wrap.style.alignItems = "center";

          const link = document.createElement("a");
          link.className = "pill";
          link.href = view.url || "#";
          link.textContent = view.name || `View ${idx + 1}`;

          const del = document.createElement("button");
          del.className = "pill";
          del.type = "button";
          del.textContent = "x";
          del.addEventListener("click", () => {
            const next = read(scope).filter((_, i) => i !== idx);
            write(scope, next);
            render();
          });

          wrap.appendChild(link);
          wrap.appendChild(del);
          list.appendChild(wrap);
        });
      }

      saveBtn.addEventListener("click", () => {
        const name = nameInput ? nameInput.value.trim() : "";
        const url = window.location.pathname + window.location.search;
        const views = read(scope);
        views.unshift({ name: name || "Saved view", url, saved_at: Date.now() });
        write(scope, views);
        if (nameInput) nameInput.value = "";
        render();
      });

      render();
    });
  }

  function setupFeedInspector() {
    const root = document.querySelector("[data-feed-inspector]");
    const inspector = root?.querySelector(".feed-inspector");
    const rows = root ? Array.from(root.querySelectorAll(".feed-signal-card[data-doc-id]")) : [];
    if (!root || !inspector) return;
    const one = (selector) => inspector.querySelector(selector);
    const all = (selector) => Array.from(inspector.querySelectorAll(selector));
    const cache = new Map();
    let request = null;
    let fullRequest = null;
    let loadTimer = null;
    const status = one("[data-inspector-status]");
    const retry = one("[data-inspector-retry]");
    const setText = (selector, value) => { const node = one(selector); if (node) node.textContent = value || "—"; };
    function setLink(selector, href) {
      const url = href ? safeHref(href) : "";
      all(selector).forEach((node) => { node.hidden = !url; if (url) node.href = url; else node.removeAttribute("href"); });
    }
    function renderLinks(selector, items, empty) {
      const wrap = one(selector); wrap.replaceChildren();
      if (!items.length) { const p = document.createElement("p"); p.textContent = empty; wrap.appendChild(p); return; }
      items.forEach((item) => {
        const href = safeHref(item.href); if (!href) return;
        const block = document.createElement("div"); block.className = "feed-research-item";
        const link = document.createElement("a"); link.href = href; link.target = "_blank"; link.rel = "noopener noreferrer"; link.textContent = item.title || item.label || href; block.appendChild(link);
        [item.polarity, item.snippet, item.note].filter(Boolean).forEach((text) => { const p = document.createElement("p"); p.textContent = text; block.appendChild(p); });
        wrap.appendChild(block);
      });
    }
    function renderMedia(items) {
      const wrap = one("[data-inspector-media]"); const section = wrap.closest(".feed-inspector-media");
      wrap.replaceChildren(); section.hidden = !items.length;
      items.forEach((item) => {
        const src = safeHref(item.src); if (!src) return;
        const link = document.createElement("a"); link.href = safeHref(item.open_url) || src; link.target = "_blank"; link.rel = "noopener noreferrer";
        const img = document.createElement("img"); img.alt = item.label || "Source image"; img.referrerPolicy = "no-referrer"; img.loading = "lazy";
        img.addEventListener("error", () => { link.remove(); section.hidden = !wrap.children.length; });
        img.src = src; link.appendChild(img); wrap.appendChild(link);
      });
      section.hidden = !wrap.children.length;
    }
    function renderReader(reader) {
      setText("[data-inspector-text]", reader.text || "No readable text was captured. Use the original source or attachments below.");
      setText("[data-inspector-reader-status]", reader.clipped ? `Showing the initial ${reader.initial_chars.toLocaleString()} of ${reader.total_chars.toLocaleString()} characters.` : `${Number(reader.total_chars || 0).toLocaleString()} captured characters${reader.has_translation ? " · English translation" : ""}`);
      one("[data-inspector-load-full]").hidden = !reader.clipped;
      one("[data-inspector-original-wrap]").hidden = !reader.has_translation;
      one("[data-inspector-original-wrap]").open = false;
      setText("[data-inspector-original]", reader.original_text || "");
    }
    function renderPayload(payload) {
      feedSelection.payload = payload;
      if (payload.doc) updateDocState(payload.doc);
      renderReader(payload.reader || {});
      const compression = (payload.analysis || []).find((item) => item.kind === "Compression" && item.text && !["failed", "error"].includes(item.status));
      setText("[data-inspector-brief-label]", compression ? "Stored research brief" : "Captured source excerpt");
      const text = compression?.text || payload.reader?.text || feedSelection.row?.querySelector(".excerpt")?.textContent || "No captured text is available.";
      setText("[data-inspector-excerpt]", text.length > 3600 ? `${text.slice(0, 3600).trim()}…\n\nContinue reading in Source.` : text);
      renderMedia(payload.media || []);
      renderLinks("[data-inspector-attachments]", [...(payload.attachments || []), ...(payload.external_links || []).map((href) => ({ href, title: href }))], "No attachments or extracted links.");
      renderLinks("[data-inspector-notes]", payload.notes || [], "No linked notes yet.");
      renderLinks("[data-inspector-evidence]", payload.evidence || [], "No thesis evidence linked yet.");
      setText("[data-inspector-linked-count]", `${(payload.notes || []).length} notes · ${(payload.evidence || []).length} thesis links`);
      const analysisWrap = one("[data-inspector-analysis]"); analysisWrap.replaceChildren();
      if (!(payload.analysis || []).length) { const p = document.createElement("p"); p.textContent = "No stored analysis for this signal. Add your interpretation as a linked note, or create a research gap to track the work needed."; analysisWrap.appendChild(p); }
      (payload.analysis || []).forEach((item) => {
        const block = document.createElement("div"); block.className = "feed-analysis-item";
        const meta = document.createElement("div"); meta.className = "feed-analysis-meta"; meta.textContent = `${item.kind} · ${item.model || "model unknown"} · ${item.status} · ${item.updated_at || ""}`; block.appendChild(meta);
        const p = document.createElement("p"); p.textContent = item.text || "No summary stored."; block.appendChild(p);
        if (item.detail) { const details = document.createElement("details"); const summary = document.createElement("summary"); summary.textContent = "Detailed analysis"; const text = document.createElement("p"); text.textContent = item.detail; details.append(summary, text); block.appendChild(details); }
        analysisWrap.appendChild(block);
      });
      status.textContent = ""; retry.hidden = true;
    }
    async function loadSelected(force) {
      const row = feedSelection.row; if (!row) return;
      const id = row.dataset.docId;
      request?.abort();
      if (!force && cache.has(id)) { renderPayload(cache.get(id)); return; }
      const controller = new AbortController(); request = controller;
      const deadline = setTimeout(() => controller.abort(new DOMException("Loading timed out. Retry this signal.", "TimeoutError")), 45000);
      status.textContent = "Loading captured text and linked research…"; retry.hidden = true;
      try {
        const payload = await responseJson(await fetch(`/api/feed/documents/${id}`, { headers: presenceHeaders({ Accept: "application/json" }), signal: controller.signal }));
        if (feedSelection.row !== row) return;
        cache.delete(id); cache.set(id, payload); while (cache.size > 4) cache.delete(cache.keys().next().value);
        renderPayload(payload);
      } catch (error) {
        if (isAbortError(error) || feedSelection.row !== row) return;
        status.textContent = `Could not load this signal: ${error.message}`; retry.hidden = false;
      } finally { clearTimeout(deadline); if (request === controller) request = null; }
    }
    function selectRow(row, options = {}) {
      if (!row) return;
      const same = feedSelection.row === row;
      rows.forEach((candidate) => { candidate.classList.toggle("selected", candidate === row); candidate.setAttribute("aria-current", candidate === row ? "true" : "false"); });
      feedSelection.row = row;
      const index = rows.indexOf(row);
      setText("[data-inspector-title]", row.querySelector("h3")?.textContent.trim() || `Document ${row.dataset.docId}`);
      setText("[data-inspector-source]", row.querySelector(".feed-source-name")?.textContent.trim());
      setText("[data-inspector-date]", row.querySelector("[data-utc]")?.textContent.trim());
      setText("[data-inspector-language]", row.querySelector(".feed-mini-pill:not(.feed-triage-state)")?.textContent.trim() || "Language unknown");
      setText("[data-inspector-position]", `${index + 1} / ${rows.length}`);
      setText("[data-inspector-source-summary]", `Document #${row.dataset.docId} · ${row.querySelector(".feed-source-name")?.textContent.trim() || "Unknown source"}. Captured text below; original source retains its authoritative rendering.`);
      setLink("[data-inspector-source-link]", row.dataset.sourceUrl);
      setLink("[data-inspector-doc-link]", row.dataset.href);
      const tagWrap = one("[data-inspector-tags]"); tagWrap.replaceChildren();
      const tags = Array.from(row.querySelectorAll(".domains span.pill")).map((node) => node.textContent.trim());
      (tags.length ? tags : ["Unclassified"]).forEach((tag) => { const node = document.createElement("span"); node.textContent = tag; tagWrap.appendChild(node); });
      all("[data-inspector-action], [data-inspector-doc-action]").forEach((node) => { node.dataset.docId = row.dataset.docId; node.disabled = false; });
      updateDocState({ id: row.dataset.docId });
      all("[data-inspector-prev]").forEach((button) => { button.disabled = index === 0; });
      all("[data-inspector-next]").forEach((button) => { button.disabled = index === rows.length - 1; });
      if (!same || options.force) {
        clearTimeout(loadTimer); request?.abort(); fullRequest?.abort(); feedSelection.payload = null;
        status.textContent = "Loading captured text and linked research…"; retry.hidden = true;
        setText("[data-inspector-brief-label]", "Source excerpt"); setText("[data-inspector-excerpt]", row.querySelector(".excerpt")?.textContent.trim());
        ["[data-inspector-text]", "[data-inspector-original]", "[data-inspector-reader-status]", "[data-inspector-linked-count]"].forEach((selector) => setText(selector, "Loading…"));
        ["[data-inspector-notes]", "[data-inspector-evidence]", "[data-inspector-analysis]", "[data-inspector-attachments]"].forEach((selector) => { one(selector).replaceChildren(); });
        one("[data-inspector-load-full]").hidden = true; one("[data-inspector-original-wrap]").hidden = true;
        renderMedia(row.dataset.thumbUrl ? [{ src: row.dataset.thumbUrl, label: "Source image" }] : []);
        loadTimer = setTimeout(() => loadSelected(options.force), 120);
        one(".feed-inspector-content").scrollTop = 0;
      }
      if (options.open !== false) inspector.classList.add("is-open");
      if (options.scroll) row.scrollIntoView({ block: "nearest" });
      window.dispatchEvent(new CustomEvent("thoth:feed-selection", { detail: { row, index } }));
    }
    window.__THOTH_SELECT_FEED_ROW = selectRow;
    function move(delta) { const index = rows.indexOf(feedSelection.row); selectRow(rows[Math.max(0, Math.min(rows.length - 1, index + delta))], { scroll: true }); }
    all("[data-inspector-prev]").forEach((button) => button.addEventListener("click", () => move(-1)));
    all("[data-inspector-next]").forEach((button) => button.addEventListener("click", () => move(1)));
    one("[data-inspector-close]").addEventListener("click", () => { inspector.classList.remove("is-open"); feedSelection.row?.focus(); });
    document.addEventListener("keydown", (event) => { if (event.key === "Escape" && !window.__THOTH_OVERLAY_OPEN) { inspector.classList.remove("is-open"); feedSelection.row?.focus(); } });
    all("[data-inspector-tab]").forEach((button, index, buttons) => {
      button.addEventListener("click", () => {
        const name = button.dataset.inspectorTab;
        buttons.forEach((candidate) => { const active = candidate === button; candidate.classList.toggle("active", active); candidate.setAttribute("aria-selected", active ? "true" : "false"); candidate.tabIndex = active ? 0 : -1; });
        all("[data-inspector-pane]").forEach((pane) => pane.classList.toggle("active", pane.dataset.inspectorPane === name));
      });
      button.tabIndex = index ? -1 : 0;
      button.addEventListener("keydown", (event) => { if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return; event.preventDefault(); const next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1 : (index + (event.key === "ArrowRight" ? 1 : -1) + buttons.length) % buttons.length; buttons[next].click(); buttons[next].focus(); });
    });
    retry.addEventListener("click", () => loadSelected(true));
    one("[data-inspector-load-full]").addEventListener("click", async (event) => {
      const row = feedSelection.row; if (!row) return;
      const button = event.currentTarget; button.disabled = true; status.textContent = "Loading complete captured text…";
      fullRequest?.abort();
      const controller = new AbortController(); fullRequest = controller;
      const deadline = setTimeout(() => controller.abort(new DOMException("Loading timed out. Retry complete text.", "TimeoutError")), 45000);
      try {
        const payload = await responseJson(await fetch(`/api/docs/${row.dataset.docId}/text`, { headers: presenceHeaders({ Accept: "application/json" }), signal: controller.signal }));
        if (feedSelection.row === row) { renderReader(payload.reader); status.textContent = ""; }
      } catch (error) { if (!isAbortError(error) && feedSelection.row === row) status.textContent = error.message; }
      finally { clearTimeout(deadline); if (fullRequest === controller) fullRequest = null; button.disabled = false; }
    });
    function pauseReads() { clearTimeout(loadTimer); request?.abort(); fullRequest?.abort(); }
    window.addEventListener("pagehide", pauseReads);
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) pauseReads();
      else if (feedSelection.row && !feedSelection.payload) loadSelected(false);
    });
    window.addEventListener("pageshow", (event) => { if (event.persisted && feedSelection.row && !feedSelection.payload) loadSelected(false); });
    window.addEventListener("thoth:feed-research-saved", (event) => { cache.delete(String(event.detail.docId)); if (feedSelection.row?.dataset.docId === String(event.detail.docId)) loadSelected(true); });
    root.querySelector("[data-feed-refresh]")?.addEventListener("click", () => window.location.reload());
    root.querySelectorAll(".feed-card-media img").forEach((img) => {
      const remove = () => { const row = img.closest(".feed-signal-card"); row.classList.remove("has-media"); row.dataset.thumbUrl = ""; img.parentElement.remove(); };
      img.addEventListener("error", remove); if (img.complete && !img.naturalWidth) remove();
    });
    if (rows.length) selectRow(rows[0], { open: false });
    else { all("button").forEach((button) => { button.disabled = true; }); setText("[data-inspector-title]", "No signal selected"); }
  }

  function setupFeedQueryHighlight() {
    const containers = Array.from(document.querySelectorAll("[data-query]"));
    if (!containers.length) return;

    function highlightText(el, terms) {
      if (!el || !terms.length) return;
      const raw = el.dataset.rawText || el.textContent || "";
      el.dataset.rawText = raw;
      const re = new RegExp(`(${terms.map((term) => term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|")})`, "gi");
      el.replaceChildren();
      raw.split(re).forEach((text, index) => {
        if (index % 2) { const mark = document.createElement("mark"); mark.textContent = text; el.appendChild(mark); }
        else el.appendChild(document.createTextNode(text));
      });
    }

    containers.forEach((container) => {
      const q = (container.getAttribute("data-query") || "").trim();
      if (!q) return;
      const terms = q.split(/\s+/).filter(Boolean).slice(0, 6);
      const targets = container.querySelectorAll(".doc h3 a, .excerpt");
      targets.forEach((el) => highlightText(el, terms));
    });
  }

  function setupFeedSeenTracking() {
    const config = window.THOTH_CONFIG || {};
    if (!config.enableSeen) return;
    const docs = Array.from(document.querySelectorAll(".doc[data-doc-id]"));
    if (docs.length === 0) return;

    const dwellMs = Number(config.seenDwellMs || 2000);
    const scrollWindowMs = Number(config.seenScrollWindowS || 10) * 1000;
    const afkMs = Number(config.seenAfkS || 30) * 1000;

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
      return now - Math.max(lastScrollAt, lastNavAt) <= scrollWindowMs;
    }

    const seenQueue = new Set();
    let seenFlushTimer = null;
    let seenFlushing = false;
    let seenFailures = 0;
    let seenRequest = null;

    function scheduleSeenFlush() {
      if (seenFlushTimer || document.hidden || seenFailures >= 3) return;
      seenFlushTimer = setTimeout(() => {
        flushSeen();
      }, 700 * (2 ** seenFailures));
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
      const controller = new AbortController(); seenRequest = controller;
      const deadline = setTimeout(() => controller.abort(), 45000);
      try {
        const res = await postJson("/documents/bulk", { doc_ids: ids, action: "seen" }, controller.signal);
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
        seenFailures = seenQueue.size ? seenFailures + 1 : 0;
      } catch (_e) { seenFailures += 1; }
      finally { clearTimeout(deadline); if (seenRequest === controller) seenRequest = null; }

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
          el.__seenTimer = setTimeout(() => {
            el.__seenTimer = null;
            if (el.dataset.seenSent === "1") return;
            if (!shouldMarkSeen()) return;
            const docId = el.getAttribute("data-doc-id");
            if (docId) enqueueSeen(docId, el);
          }, dwellMs);
        });
      },
      { threshold: [0.6] }
    );

    docs.forEach((doc) => observer.observe(doc));
    function pauseSeen() {
      observer.disconnect();
      clearTimeout(seenFlushTimer); seenFlushTimer = null;
      seenRequest?.abort();
      docs.forEach((doc) => { clearTimeout(doc.__seenTimer); doc.__seenTimer = null; });
    }
    function resumeSeen() {
      if (document.hidden) return;
      seenFailures = 0;
      docs.forEach((doc) => observer.observe(doc));
      if (seenQueue.size) scheduleSeenFlush();
    }
    document.addEventListener("visibilitychange", () => { if (document.hidden) pauseSeen(); else resumeSeen(); });
    window.addEventListener("pagehide", pauseSeen);
    window.addEventListener("pageshow", (event) => { if (event.persisted) resumeSeen(); });
  }

  function setupFeedKeyboardAndTriage() {
    const docEls = Array.from(document.querySelectorAll(".doc[data-doc-id]"));
    const triageButtons = Array.from(document.querySelectorAll("button.doc-triage[data-doc-action]"));
    if (docEls.length === 0 && triageButtons.length === 0) return;

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
        const nodes = Array.from(document.querySelectorAll(`button.doc-triage[data-doc-id="${Number(docId)}"]`));
        if (nodes.some((node) => node.disabled)) return;
        nodes.forEach((node) => { node.disabled = true; });
        try {
          const receipt = await responseJson(await postForm(`/documents/${encodeURIComponent(docId)}/${act}`, {}));
          if (receipt.doc) updateDocState(receipt.doc);
          quickToast(act === "save" ? "Saved." : act === "unsave" ? "Removed from saved." : "Triage updated.");
        } finally { nodes.forEach((node) => { node.disabled = false; }); }
      } catch (error) { quickToast(error.message); }
    }

    document.addEventListener("click", (e) => {
      if (!isFeedRoute()) return;
      const btn = e.target && e.target.closest ? e.target.closest("button.doc-triage[data-doc-action]") : null;
      if (!btn) return;
      const action = (btn.getAttribute("data-doc-action") || "").toLowerCase();
      const docId = btn.getAttribute("data-doc-id") || btn.closest(".doc")?.getAttribute("data-doc-id");
      if (docId) applyAction(docId, action);
    });

    if (docEls.length === 0) return;

    let idx = 0;
    const lastNavRef = { ts: 0 };
    function setSelected(i, opts) {
      const safe = Math.max(0, Math.min(i, docEls.length - 1));
      idx = safe;
      const el = docEls[idx];
      if (el) {
        if (window.__THOTH_SELECT_FEED_ROW) window.__THOTH_SELECT_FEED_ROW(el, opts || {});
        else {
          docEls.forEach((doc) => doc.classList.remove("selected"));
          el.classList.add("selected");
          if (opts && opts.scroll) el.scrollIntoView({ block: "nearest" });
        }
      }
    }
    window.addEventListener("thoth:feed-selection", (event) => {
      const nextIndex = Number(event.detail?.index);
      if (Number.isFinite(nextIndex)) idx = nextIndex;
    });
    setSelected(0, { scroll: false, open: false });

    document.addEventListener("keydown", (e) => {
      if (window.__THOTH_OVERLAY_OPEN) return;
      if (!isFeedRoute()) return;
      const tag = ((e.target && e.target.tagName) || "").toLowerCase();
      if (["input", "textarea", "select", "button", "a", "summary"].includes(tag) || e.target?.isContentEditable || e.ctrlKey || e.metaKey || e.altKey) return;

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
        if (el && window.__THOTH_SELECT_FEED_ROW) {
          e.preventDefault();
          window.__THOTH_SELECT_FEED_ROW(el);
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

    window.__THOTH_LAST_NAV = lastNavRef;
  }

  function sameOriginDocHref(href) {
    try {
      const url = new URL(href, window.location.href);
      if (url.origin !== window.location.origin) return "";
      if (!/^\/docs\/\d+/.test(url.pathname)) return "";
      return `${url.pathname}${url.search || ""}`;
    } catch (_e) {
      return "";
    }
  }

  function setupFeedRowClicks() {
    document.addEventListener("click", (event) => {
      const row = event.target.closest(".feed-signal-card[data-href]");
      if (!row || event.target.closest(".feed-select-box")) return;
      const link = event.target.closest("a[href]");
      if (link && !sameOriginDocHref(link.getAttribute("href"))) return;
      if (!link && event.target.closest("button, input, select, textarea, summary, details")) return;
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || link?.target === "_blank") {
        if (!link && (event.metaKey || event.ctrlKey)) window.open(row.dataset.href, "_blank", "noopener");
        return;
      }
      event.preventDefault();
      window.__THOTH_SELECT_FEED_ROW?.(row);
    });
  }

  setupEarningsRailToggle();
  setupFeedSavedViews();
  setupFeedInspector();
  setupFeedQueryHighlight();
  setupFeedSeenTracking();
  setupFeedKeyboardAndTriage();
  setupEvidenceModal();
  setupBulkActions();
  setupFeedDocActions();
  setupFeedRowClicks();
})();
