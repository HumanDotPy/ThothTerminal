(function () {
  function toast(message) {
    if (typeof window.quickToast === "function") {
      window.quickToast(message);
    }
  }

  function init() {
    const loadout = document.getElementById("doc-reader-loadout");
    const btn = document.getElementById("doc-load-full-text");
    if (!loadout || !btn || btn.dataset.readerBound === "1") return;
    const docId = loadout.dataset.docId;
    const main = document.querySelector("[data-doc-reader-main]");
    const original = document.querySelector("[data-doc-reader-original]");
    if (!docId || !main) return;
    btn.dataset.readerBound = "1";
    if (btn.dataset.readerPendingBind === "1") {
      btn.disabled = false;
      delete btn.dataset.readerPendingBind;
    }

    btn.addEventListener("click", async () => {
      btn.disabled = true;
      const previous = btn.textContent;
      btn.textContent = "Loading...";
      try {
        const res = await fetch(`/api/docs/${encodeURIComponent(docId)}/text`, {
          headers: { "X-Thoth-Purpose": "reader-full-text" },
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const json = await res.json();
        if (!json.ok || !json.reader) throw new Error(json.error || "load_failed");
        main.textContent = json.reader.text || "";
        if (original && typeof json.reader.original_text === "string") {
          original.textContent = json.reader.original_text;
        }
        loadout.style.display = "none";
      } catch (_e) {
        btn.disabled = false;
        btn.textContent = previous || "Load full text";
        toast("Full text load failed");
      }
    });
  }

  window.ThothDocReader = Object.assign(window.ThothDocReader || {}, { init });
  init();
})();
