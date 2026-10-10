(function () {
  const editor = document.getElementById("note-editor");
  if (!editor) return;

  const THOTH_CONFIG = window.THOTH_CONFIG || {};
  const autosaveConfigMs = Number(THOTH_CONFIG.autosaveIntervalMs || 450);
  const AUTOSAVE_MS = Number.isFinite(autosaveConfigMs) ? Math.max(250, autosaveConfigMs) : 450;

  function formEncode(fields) {
    const params = new URLSearchParams();
    Object.entries(fields).forEach(([k, v]) => params.set(k, v == null ? "" : String(v)));
    return params;
  }

  function isAbortError(error) {
    return error && error.name === "AbortError";
  }

  function quickToast(message) {
    if (typeof window.quickToast === "function") {
      window.quickToast(message);
    }
  }

  const noteId = editor.dataset.noteId;
  const titleEl = document.getElementById("note-title");
  const bodyEl = document.getElementById("note-body");
  const statusEl = document.getElementById("note-save-status");
  const previewEl = document.getElementById("note-preview");

  if (!noteId || !titleEl || !bodyEl || !statusEl) return;

  let timer = null;
  let saveSeq = 0;
  const historyEl = document.getElementById("note-history");
  const historyKey = `thoth_note_history_${noteId}`;
  const NOTE_HISTORY_ENTRY_LIMIT = 6;
  const NOTE_HISTORY_BODY_CHAR_LIMIT = 20000;
  const NOTE_HISTORY_PREVIEW_CHAR_LIMIT = 600;
  const NOTE_HISTORY_TOTAL_CHAR_LIMIT = 140000;

  function setStatus(text) {
    statusEl.textContent = text;
  }

  function bodyFingerprint(body) {
    const value = String(body || "");
    return `${value.length}:${value.slice(0, 256)}:${value.slice(-256)}`;
  }

  function normalizeHistoryEntry(entry) {
    const raw = entry && typeof entry === "object" ? entry : {};
    const title = String(raw.title || "").slice(0, 512);
    const rawBody = typeof raw.body === "string" ? raw.body : "";
    const bodyTooLarge = rawBody.length > NOTE_HISTORY_BODY_CHAR_LIMIT || raw.body_truncated === true;
    const previewSource = rawBody || String(raw.body_preview || "");
    return {
      title,
      body: bodyTooLarge ? "" : rawBody,
      body_preview: previewSource.slice(0, NOTE_HISTORY_PREVIEW_CHAR_LIMIT),
      body_truncated: bodyTooLarge,
      body_length: Number(raw.body_length || rawBody.length || previewSource.length || 0),
      body_fingerprint: String(raw.body_fingerprint || bodyFingerprint(rawBody || previewSource)),
      saved_at: Number(raw.saved_at || Date.now()),
    };
  }

  function trimHistory(entries) {
    const normalized = (Array.isArray(entries) ? entries : []).map(normalizeHistoryEntry).slice(0, NOTE_HISTORY_ENTRY_LIMIT);
    while (normalized.length > 1 && JSON.stringify(normalized).length > NOTE_HISTORY_TOTAL_CHAR_LIMIT) {
      normalized.pop();
    }
    return normalized;
  }

  function readHistory() {
    try {
      const raw = localStorage.getItem(historyKey);
      const entries = raw ? trimHistory(JSON.parse(raw)) : [];
      const normalized = JSON.stringify(entries);
      if (raw && raw !== normalized) localStorage.setItem(historyKey, normalized);
      return entries;
    } catch (_e) {
      return [];
    }
  }

  function writeHistory(entries) {
    try {
      localStorage.setItem(historyKey, JSON.stringify(trimHistory(entries)));
    } catch (_e) {}
  }

  function renderHistory() {
    if (!historyEl) return;
    const entries = readHistory();
    historyEl.innerHTML = "";
    if (!entries.length) {
      const empty = document.createElement("div");
      empty.style.color = "var(--muted)";
      empty.style.fontFamily = "var(--mono)";
      empty.style.fontSize = "12px";
      empty.textContent = "No snapshots yet.";
      historyEl.appendChild(empty);
      return;
    }
    entries.forEach((entry) => {
      const row = document.createElement("div");
      row.className = "link-row";
      row.style.alignItems = "center";

      const left = document.createElement("div");
      const ts = new Date(entry.saved_at || Date.now());
      const title = entry.title || "(untitled)";
      const snippet = (entry.body_preview || entry.body || "").slice(0, 80);
      const canRestore = entry.body_truncated !== true && typeof entry.body === "string";
      left.innerHTML = `<div style="font-size:12px;">${title}</div><div style="color:var(--muted);font-family:var(--mono);font-size:11px;">${ts.toLocaleString()} · ${snippet}${snippet.length >= 80 ? "…" : ""}</div>`;

      const btn = document.createElement("button");
      btn.className = "btn";
      btn.type = "button";
      btn.textContent = "Restore";
      btn.disabled = !canRestore;
      if (!canRestore) btn.title = "Snapshot body exceeds local restore cache.";
      if (canRestore) {
        btn.addEventListener("click", () => {
          titleEl.value = entry.title || "";
          bodyEl.value = entry.body || "";
          scheduleSave();
        });
      }

      row.appendChild(left);
      row.appendChild(btn);
      historyEl.appendChild(row);
    });
  }

  function pushHistory() {
    const entries = readHistory();
    const title = titleEl.value || "";
    const body = bodyEl.value || "";
    const next = normalizeHistoryEntry({
      title,
      body,
      body_preview: body.slice(0, NOTE_HISTORY_PREVIEW_CHAR_LIMIT),
      body_truncated: body.length > NOTE_HISTORY_BODY_CHAR_LIMIT,
      body_length: body.length,
      body_fingerprint: bodyFingerprint(body),
      saved_at: Date.now(),
    });
    const last = entries[0];
    if (last && last.title === next.title && last.body_fingerprint === next.body_fingerprint) return;
    entries.unshift(next);
    writeHistory(entries);
    renderHistory();
  }

  async function saveNow() {
    const mySeq = ++saveSeq;
    setStatus("saving…");
    try {
      const res = await fetch(`/api/notes/${encodeURIComponent(noteId)}/autosave`, {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: formEncode({ title: titleEl.value || "", body_md: bodyEl.value || "" }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json = await res.json();
      if (mySeq !== saveSeq) return;
      if (previewEl && typeof json.preview_html === "string") {
        previewEl.innerHTML = json.preview_html;
      }
      setStatus("saved");
      pushHistory();
    } catch (_e) {
      setStatus("error");
    }
  }

  function scheduleSave() {
    clearTimeout(timer);
    timer = setTimeout(saveNow, AUTOSAVE_MS);
    setStatus("saving…");
  }

  titleEl.addEventListener("input", scheduleSave);
  bodyEl.addEventListener("input", scheduleSave);
  renderHistory();

  function insertAtCursor(textarea, token) {
    const start = textarea.selectionStart || 0;
    const end = textarea.selectionEnd || 0;
    const before = textarea.value.slice(0, start);
    const after = textarea.value.slice(end);
    textarea.value = before + token + after;
    const next = start + token.length;
    textarea.setSelectionRange(next, next);
    textarea.focus();
  }

  const attachInput = document.getElementById("note-attach-input");
  const attachAlt = document.getElementById("note-attach-alt");
  const attachGrid = document.getElementById("note-attachments-grid");
  const attachEmpty = document.getElementById("note-attachments-empty");

  function attachInsert(path, altText) {
    const alt = altText || "image";
    insertAtCursor(bodyEl, `![${alt}](/objects/${path})`);
    scheduleSave();
  }

  function bindAttachmentThumb(btn) {
    if (!btn) return;
    btn.addEventListener("click", () => {
      const path = btn.dataset.attachmentPath || "";
      const alt = btn.dataset.attachmentAlt || "";
      if (!path) return;
      attachInsert(path, alt);
    });
  }

  if (attachGrid) {
    attachGrid.querySelectorAll(".note-attachment-thumb").forEach(bindAttachmentThumb);
  }

  if (attachInput) {
    attachInput.addEventListener("change", async () => {
      const file = attachInput.files && attachInput.files[0];
      if (!file) return;
      const form = new FormData();
      form.append("file", file);
      if (attachAlt && attachAlt.value) form.append("alt", attachAlt.value);
      try {
        const res = await fetch(`/api/notes/${encodeURIComponent(noteId)}/attachments`, {
          method: "POST",
          body: form,
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const json = await res.json();
        if (!json.ok) throw new Error(json.error || "upload_failed");
        if (json.attachment && json.attachment.object_path) {
          const path = json.attachment.object_path;
          const alt = json.attachment.alt || (attachAlt && attachAlt.value) || file.name || "image";
          attachInsert(path, alt);
          if (attachGrid) {
            const btn = document.createElement("button");
            btn.type = "button";
            btn.className = "note-attachment-thumb";
            btn.dataset.attachmentPath = path;
            btn.dataset.attachmentAlt = alt;
            btn.innerHTML = `<img src="${json.url || `/objects/${path}`}" alt="" loading="lazy" />`;
            bindAttachmentThumb(btn);
            attachGrid.appendChild(btn);
          }
          if (attachEmpty) attachEmpty.style.display = "none";
        }
        if (attachAlt) attachAlt.value = "";
        attachInput.value = "";
        quickToast("Attachment added");
      } catch (_e) {
        quickToast("Attachment upload failed");
      }
    });
  }

  function setupCitation(opts) {
    const btn = opts.btn;
    const overlay = opts.overlay;
    const close = opts.close;
    const cancel = opts.cancel;
    const search = opts.search;
    const results = opts.results;
    const kind = opts.kind;
    const tokenPrefix = opts.tokenPrefix;
    const linkUrl = opts.linkUrl;
    const linkField = opts.linkField;
    const linkRelation = opts.linkRelation;

    if (!btn || !overlay || !search || !results) return;

    function hide() {
      overlay.style.display = "none";
      search.value = "";
      results.innerHTML = "";
    }

    function show() {
      overlay.style.display = "block";
      search.focus();
    }

    btn.addEventListener("click", show);
    if (close) close.addEventListener("click", hide);
    if (cancel) cancel.addEventListener("click", hide);
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) hide();
    });

    let searchTimer = null;
    let qid = 0;
    let searchAbort = null;

    function abortSearch() {
      if (searchAbort) {
        searchAbort.abort();
        searchAbort = null;
      }
    }

    search.addEventListener("input", () => {
      clearTimeout(searchTimer);
      const q = (search.value || "").trim();
      results.innerHTML = "";
      if (!q) {
        abortSearch();
        return;
      }
      searchTimer = setTimeout(async () => {
        const myId = ++qid;
        abortSearch();
        const controller = new AbortController();
        searchAbort = controller;
        try {
          const res = await fetch(`/api/search?kind=${encodeURIComponent(kind)}&q=${encodeURIComponent(q)}&limit=12`, { signal: controller.signal });
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          const json = await res.json();
          if (myId !== qid) return;
          const items = Array.isArray(json.items) ? json.items : [];
          results.innerHTML = "";
          items.forEach((it) => {
            const el = document.createElement("div");
            el.className = "search-item";
            el.textContent = it.label || String(it.id);
            el.addEventListener("click", async () => {
              const id = String(it.id);
              insertAtCursor(bodyEl, `[[${tokenPrefix}:${id}]]`);
              scheduleSave();
              if (linkUrl && linkField) {
                const payload = {};
                payload[linkField] = id;
                if (linkRelation) payload.relation = linkRelation;
                try {
                  await fetch(linkUrl, {
                    method: "POST",
                    headers: { "Content-Type": "application/x-www-form-urlencoded" },
                    body: formEncode(payload),
                  });
                } catch (_e) {}
              }
              hide();
            });
            results.appendChild(el);
          });
        } catch (error) {
          if (isAbortError(error)) return;
          results.innerHTML = "";
        } finally {
          if (searchAbort === controller) searchAbort = null;
        }
      }, 180);
    });
  }

  setupCitation({
    btn: document.getElementById("note-cite-doc"),
    overlay: document.getElementById("note-cite-overlay"),
    close: document.getElementById("note-cite-close"),
    cancel: document.getElementById("note-cite-cancel"),
    search: document.getElementById("note-cite-search"),
    results: document.getElementById("note-cite-results"),
    kind: "doc",
    tokenPrefix: "doc",
    linkUrl: `/notes/${encodeURIComponent(noteId)}/link/document`,
    linkField: "document_id",
  });

  setupCitation({
    btn: document.getElementById("note-cite-thesis"),
    overlay: document.getElementById("note-cite-thesis-overlay"),
    close: document.getElementById("note-cite-thesis-close"),
    cancel: document.getElementById("note-cite-thesis-cancel"),
    search: document.getElementById("note-cite-thesis-search"),
    results: document.getElementById("note-cite-thesis-results"),
    kind: "thesis",
    tokenPrefix: "thesis",
    linkUrl: `/notes/${encodeURIComponent(noteId)}/link/thesis`,
    linkField: "thesis_id",
    linkRelation: "context",
  });
})();
