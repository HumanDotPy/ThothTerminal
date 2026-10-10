(function () {
  const rows = Array.from(document.querySelectorAll("[data-agent-work-row]"));
  const inspector = document.querySelector("[data-agent-work-inspector]");
  const drawer = document.querySelector("[data-agent-intake-drawer]");

  function write(selector, value) {
    const node = inspector ? inspector.querySelector(selector) : null;
    if (node) node.textContent = value || "—";
  }

  function selectRow(row, options) {
    if (!row || !inspector) return;
    rows.forEach((candidate) => candidate.classList.toggle("selected", candidate === row));
    const index = rows.indexOf(row);
    write("[data-agent-position]", `${index + 1} / ${rows.length}`);
    write("[data-agent-title]", row.dataset.title || "Untitled work item");
    write("[data-agent-meta]", `${row.dataset.label || "Work item"} / ${row.dataset.created || "time unknown"}`);
    write("[data-agent-summary]", row.dataset.summary || "No summary is attached.");
    write("[data-agent-kind]", row.dataset.kind || "unknown");
    write("[data-agent-created]", row.dataset.created || "unknown");
    write("[data-agent-label]", row.dataset.label || "unknown");
    const open = inspector.querySelector("[data-agent-open]");
    if (open) {
      const href = row.dataset.href || "";
      open.href = href || "#";
      open.classList.toggle("is-disabled", !href);
      open.setAttribute("aria-disabled", href ? "false" : "true");
    }
    inspector.classList.add("is-open");
    if (options && options.scroll) row.scrollIntoView({ block: "nearest" });
  }

  rows.forEach((row) => {
    row.addEventListener("click", (event) => {
      if (event.target.closest("a,button,input,select,textarea")) return;
      selectRow(row);
    });
    row.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      event.preventDefault();
      selectRow(row);
    });
  });

  document.querySelectorAll("[data-agent-tab]").forEach((button) => {
    button.addEventListener("click", () => {
      const name = button.dataset.agentTab;
      document.querySelectorAll("[data-agent-tab]").forEach((candidate) => candidate.classList.toggle("active", candidate === button));
      document.querySelectorAll("[data-agent-pane]").forEach((pane) => pane.classList.toggle("active", pane.dataset.agentPane === name));
    });
  });

  document.querySelectorAll("[data-agent-intake-toggle]").forEach((button) => {
    button.addEventListener("click", () => {
      if (!drawer) return;
      drawer.open = true;
      drawer.scrollIntoView({ block: "start", behavior: "smooth" });
    });
  });

  const search = document.querySelector("[data-agent-queue-search]");
  if (search) {
    search.addEventListener("input", () => {
      const query = search.value.trim().toLowerCase();
      rows.forEach((row) => {
        const haystack = `${row.dataset.title || ""} ${row.dataset.label || ""} ${row.dataset.summary || ""}`.toLowerCase();
        row.hidden = !!query && !haystack.includes(query);
      });
    });
  }

  if (rows.length) selectRow(rows.find((row) => row.classList.contains("selected")) || rows[0]);
})();
