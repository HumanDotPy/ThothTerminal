(function () {
  const panel = document.getElementById("thoth-operation");
  const text = document.getElementById("thoth-operation-text");
  const time = document.getElementById("thoth-operation-time");
  if (!panel || !text || !time) return;

  let active = 0;
  let startedAt = 0;
  let revealTimer = 0;
  let clockTimer = 0;
  let label = "Working";

  function copyFor(elapsed) {
    if (elapsed >= 25) return `${label}. This is taking longer than usual; Thoth is still waiting for the server.`;
    if (elapsed >= 8) return `${label}. Still working — this has not been marked as hung.`;
    return `${label}… (Waiting for something to happen?)`;
  }

  function renderClock() {
    if (!startedAt) return;
    const elapsed = Math.max(0, Math.floor((performance.now() - startedAt) / 1000));
    text.textContent = copyFor(elapsed);
    time.textContent = elapsed ? `${elapsed}s` : "";
  }

  function show(nextLabel, immediate) {
    active += 1;
    label = String(nextLabel || "Working").trim() || "Working";
    if (!startedAt) startedAt = performance.now();
    window.clearTimeout(revealTimer);
    revealTimer = window.setTimeout(() => {
      panel.hidden = false;
      panel.classList.add("is-visible");
      renderClock();
      window.clearInterval(clockTimer);
      clockTimer = window.setInterval(renderClock, 1000);
    }, immediate ? 0 : 280);
  }

  function hide() {
    active = Math.max(0, active - 1);
    if (active) return;
    window.clearTimeout(revealTimer);
    window.clearInterval(clockTimer);
    panel.classList.remove("is-visible");
    window.setTimeout(() => { if (!active) panel.hidden = true; }, 180);
    startedAt = 0;
    time.textContent = "";
  }

  window.ThothOperation = { show, hide };
  window.addEventListener("pageshow", () => {
    active = 1;
    hide();
  });

  document.addEventListener("click", (event) => {
    const anchor = event.target.closest("a[href]");
    if (!anchor || event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    if (anchor.target && anchor.target !== "_self") return;
    const url = new URL(anchor.href, window.location.href);
    if (url.origin !== window.location.origin || url.href === window.location.href || url.hash && url.pathname === window.location.pathname) return;
    show(anchor.dataset.operationLabel || "Loading", true);
  });

  document.addEventListener("submit", (event) => {
    if (event.defaultPrevented) return;
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    const method = String(form.method || "get").toLowerCase();
    show(form.dataset.operationLabel || (method === "get" ? "Loading" : "Saving changes"), true);
  });

  const nativeFetch = window.fetch.bind(window);
  window.fetch = async function (input, init) {
    const method = String(init && init.method || "GET").toUpperCase();
    const url = String(input && input.url || input || "");
    const visible = method !== "GET" || /\/api\/agents\/(?!harness\/)/.test(url);
    if (visible) show(method === "GET" ? "Refreshing agent state" : "Saving changes", false);
    try {
      return await nativeFetch(input, init);
    } finally {
      if (visible) hide();
    }
  };
})();
