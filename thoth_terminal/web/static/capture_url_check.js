(function () {
  const form = document.getElementById("capture-form");
  if (!form) return;
  const urlInput = form.querySelector('input[name="url"]');
  const statusEl = document.getElementById("capture-url-status");
  if (!urlInput || !statusEl) return;

  let timer = null;

  async function checkUrl(raw) {
    const url = (raw || "").trim();
    if (!url) {
      statusEl.textContent = "";
      statusEl.className = "pill";
      return;
    }
    statusEl.textContent = "checking…";
    statusEl.className = "pill";
    try {
      const res = await fetch(`/api/capture/check?url=${encodeURIComponent(url)}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json = await res.json();
      if (json.ok) {
        statusEl.textContent = `ok · ${json.status || "200"}`;
        statusEl.className = "pill ok";
      } else {
        statusEl.textContent = json.detail || "unreachable";
        statusEl.className = "pill bad";
      }
    } catch (_e) {
      statusEl.textContent = "check failed";
      statusEl.className = "pill bad";
    }
  }

  urlInput.addEventListener("blur", () => {
    clearTimeout(timer);
    timer = setTimeout(() => checkUrl(urlInput.value), 150);
  });
})();
