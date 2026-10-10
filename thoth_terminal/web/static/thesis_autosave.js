(function () {
  const THOTH_CONFIG = window.THOTH_CONFIG || {};
  const autosaveConfigMs = Number(THOTH_CONFIG.autosaveIntervalMs || 450);
  const AUTOSAVE_MS = Number.isFinite(autosaveConfigMs) ? Math.max(250, autosaveConfigMs) : 450;

  function formEncode(fields) {
    const params = new URLSearchParams();
    Object.entries(fields).forEach(([k, v]) => params.set(k, v == null ? "" : String(v)));
    return params;
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

  const form = document.getElementById("thesis-edit-form");
  if (!form) return;
  const thesisId = form.dataset.thesisId;
  if (!thesisId) return;
  const statusEl = document.getElementById("thesis-save-status");
  let timer = null;
  let saveSeq = 0;

  function setStatus(text) {
    if (statusEl) statusEl.textContent = text;
  }

  async function saveNow() {
    const mySeq = ++saveSeq;
    setStatus("saving…");
    try {
      const payload = collectFormValues(form);
      const res = await fetch(`/api/theses/${encodeURIComponent(thesisId)}/autosave`, {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: formEncode(payload),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      if (mySeq !== saveSeq) return;
      setStatus("saved");
    } catch (_e) {
      setStatus("error");
    }
  }

  function scheduleSave() {
    clearTimeout(timer);
    timer = setTimeout(saveNow, AUTOSAVE_MS);
    setStatus("saving…");
  }

  form.addEventListener("input", scheduleSave);
  form.addEventListener("change", scheduleSave);
})();
