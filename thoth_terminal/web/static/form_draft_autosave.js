(function () {
  const THOTH_CONFIG = window.THOTH_CONFIG || {};
  const autosaveConfigMs = Number(THOTH_CONFIG.autosaveIntervalMs || 450);
  const AUTOSAVE_MS = Number.isFinite(autosaveConfigMs) ? Math.max(250, autosaveConfigMs) : 450;

  function readDraft(key) {
    try {
      const raw = localStorage.getItem(key);
      return raw ? JSON.parse(raw) : null;
    } catch (_e) {
      return null;
    }
  }

  function writeDraft(key, payload) {
    try {
      localStorage.setItem(key, JSON.stringify(payload));
    } catch (_e) {}
  }

  function clearDraft(key) {
    try {
      localStorage.removeItem(key);
    } catch (_e) {}
  }

  function collectFormValues(form) {
    const values = {};
    Array.from(form.elements || []).forEach((el) => {
      if (!el || !el.name) return;
      if (el.type === "submit" || el.type === "button" || el.type === "file") return;
      if (el.type === "checkbox") {
        values[el.name] = el.checked ? "1" : "";
        return;
      }
      values[el.name] = el.value;
    });
    return values;
  }

  function applyFormValues(form, values) {
    if (!values) return;
    Array.from(form.elements || []).forEach((el) => {
      if (!el || !el.name) return;
      if (!(el.name in values)) return;
      if (el.type === "checkbox") {
        el.checked = Boolean(values[el.name]);
        return;
      }
      el.value = values[el.name];
    });
  }

  function syncChipSelection(containerId, inputId) {
    const container = document.getElementById(containerId);
    const input = document.getElementById(inputId);
    if (!container || !input) return;
    const selected = new Set(
      (input.value || "")
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean)
    );
    container.querySelectorAll(".chip").forEach((btn) => {
      const slug = btn.dataset.slug;
      if (!slug) return;
      if (selected.has(slug)) {
        btn.classList.add("selected");
      } else {
        btn.classList.remove("selected");
      }
    });
  }

  function setupDraftAutosave(opts) {
    const form = document.getElementById(opts.formId);
    if (!form) return;
    const key = opts.storageKey;
    const statusEl = document.getElementById(opts.statusId);
    const restoreWrap = document.getElementById(opts.restoreWrapId);
    const restoreBtn = document.getElementById(opts.restoreBtnId);
    const discardBtn = document.getElementById(opts.discardBtnId);
    const tsEl = document.getElementById(opts.restoreTsId);

    let timer = null;
    let restoring = false;

    function setStatus(text) {
      if (statusEl) statusEl.textContent = text;
    }

    function updateRestoreTimestamp(raw) {
      if (!tsEl) return;
      const ts = raw ? new Date(raw) : new Date();
      tsEl.textContent = ts.toLocaleString();
    }

    function saveDraft() {
      const fields = collectFormValues(form);
      writeDraft(key, { fields, saved_at: Date.now() });
      setStatus("draft saved");
      updateRestoreTimestamp(Date.now());
    }

    function scheduleSave() {
      if (restoring) return;
      clearTimeout(timer);
      timer = setTimeout(saveDraft, AUTOSAVE_MS);
      setStatus("draft saving…");
    }

    const existing = readDraft(key);
    if (existing && existing.fields) {
      if (restoreWrap) restoreWrap.style.display = "block";
      updateRestoreTimestamp(existing.saved_at);
      setStatus("draft available");
    } else {
      setStatus("draft idle");
    }

    if (restoreBtn && existing && existing.fields) {
      restoreBtn.addEventListener("click", () => {
        restoring = true;
        applyFormValues(form, existing.fields);
        restoring = false;
        if (typeof opts.afterRestore === "function") {
          opts.afterRestore(form, existing.fields);
        }
        if (restoreWrap) restoreWrap.style.display = "none";
        setStatus("draft restored");
      });
    }

    if (discardBtn) {
      discardBtn.addEventListener("click", () => {
        clearDraft(key);
        if (restoreWrap) restoreWrap.style.display = "none";
        setStatus("draft cleared");
      });
    }

    form.addEventListener("input", scheduleSave);
    form.addEventListener("change", scheduleSave);
    form.addEventListener("click", (e) => {
      const chip = e.target && e.target.closest ? e.target.closest(".chip") : null;
      if (chip && form.contains(chip)) {
        scheduleSave();
      }
    });
    form.addEventListener("submit", () => {
      clearDraft(key);
    });
  }

  setupDraftAutosave({
    formId: "note-new-form",
    statusId: "note-draft-status",
    storageKey: "thoth_note_new_draft",
    restoreWrapId: "note-draft-restore",
    restoreBtnId: "note-draft-restore-btn",
    discardBtnId: "note-draft-discard-btn",
    restoreTsId: "note-draft-timestamp",
  });

  setupDraftAutosave({
    formId: "thesis-new-form",
    statusId: "thesis-draft-status",
    storageKey: "thoth_thesis_new_draft",
    restoreWrapId: "thesis-draft-restore",
    restoreBtnId: "thesis-draft-restore-btn",
    discardBtnId: "thesis-draft-discard-btn",
    restoreTsId: "thesis-draft-timestamp",
    afterRestore: () => {
      syncChipSelection("domain-chips", "domains-input");
      syncChipSelection("tag-chips", "tags-input");
      syncChipSelection("event-chips", "events-input");
    },
  });
})();
