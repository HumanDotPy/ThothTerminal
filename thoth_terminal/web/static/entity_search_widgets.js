(function () {
  function isAbortError(error) {
    return error && error.name === "AbortError";
  }

  const inputs = Array.from(document.querySelectorAll("input[data-search]"));
  if (inputs.length === 0) return;

  function bind(input) {
    const kind = (input.dataset.search || "").trim();
    if (!kind) return;

    const field = input.closest(".field") || input.parentElement;
    const results = field ? field.querySelector(`.search-results[data-results="${kind}"]`) : null;
    const form = input.closest("form");
    const hidden = form ? form.querySelector(`input[data-selected-id="${kind}"]`) : null;
    if (!results || !hidden) return;

    let timer = null;
    let lastQueryId = 0;
    let searchAbort = null;

    function abortSearch() {
      if (searchAbort) {
        searchAbort.abort();
        searchAbort = null;
      }
    }

    function hide() {
      abortSearch();
      results.style.display = "none";
      results.innerHTML = "";
    }

    async function run(q) {
      const qid = ++lastQueryId;
      const url = `/api/search?kind=${encodeURIComponent(kind)}&q=${encodeURIComponent(q)}&limit=12`;
      abortSearch();
      const controller = new AbortController();
      searchAbort = controller;
      try {
        const res = await fetch(url, { signal: controller.signal });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const json = await res.json();
        if (qid !== lastQueryId) return;
        const items = Array.isArray(json.items) ? json.items : [];
        results.innerHTML = "";
        if (items.length === 0) {
          hide();
          return;
        }
        items.forEach((it) => {
          const el = document.createElement("div");
          el.className = "search-item";
          el.textContent = it.label || String(it.id);
          el.dataset.id = String(it.id);
          el.addEventListener("click", () => {
            hidden.value = String(it.id);
            input.value = it.label || String(it.id);
            hide();
          });
          results.appendChild(el);
        });
        results.style.display = "block";
      } catch (error) {
        if (isAbortError(error)) return;
        hide();
      } finally {
        if (searchAbort === controller) searchAbort = null;
      }
    }

    input.addEventListener("input", () => {
      hidden.value = "";
      const q = (input.value || "").trim();
      clearTimeout(timer);
      if (!q) {
        hide();
        return;
      }
      timer = setTimeout(() => run(q), 200);
    });

    document.addEventListener("click", (e) => {
      if (e.target === input || results.contains(e.target)) return;
      hide();
    });
  }

  inputs.forEach(bind);
})();
