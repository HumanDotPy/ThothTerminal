(function () {
  // Saved views (localStorage)
  (function () {
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
      const scope = container.getAttribute("data-saved-views") || "default";
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
          del.textContent = "×";
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
  })();

  // Query highlighting for list views
  (function () {
    const containers = Array.from(document.querySelectorAll("[data-query]"));
    if (!containers.length) return;

    function highlightText(el, terms) {
      if (!el || !terms.length) return;
      const raw = el.dataset.rawText || el.textContent || "";
      el.dataset.rawText = raw;
      let html = raw;
      terms.forEach((term) => {
        if (!term) return;
        const re = new RegExp(`(${term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")})`, "gi");
        html = html.replace(re, "<mark>$1</mark>");
      });
      el.innerHTML = html;
    }

    containers.forEach((container) => {
      const q = (container.getAttribute("data-query") || "").trim();
      if (!q) return;
      const terms = q.split(/\s+/).filter(Boolean).slice(0, 6);
      const targets = container.querySelectorAll(".doc h3 a, .note-title a, .thesis-title a, .excerpt, .note-excerpt, .thesis-statement");
      targets.forEach((el) => highlightText(el, terms));
    });
  })();
})();
