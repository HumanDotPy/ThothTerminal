(function () {
  "use strict";
  const grid = document.getElementById("review-grid"); let data; let scenario = "flows"; let mounted = []; let revision = 0;
  const selections = new Map();
  let providerFocus = null; let modelDetail = "families";
  function text(tag, cls, value) { const e = document.createElement(tag); if (cls) e.className = cls; e.textContent = value; return e; }
  const scenarios = {
    flows: { description: "How much is moving through US memory trade? Compare absolute dollar values with relative change. These are trade values for HS 854232, not memory capacity or domestic consumption.", cells: [{ id: "A1", key: "flows", title: "Memory imports and exports", sub: "Monthly trade value · USD", scale: "linear", choices: [["linear", "Linear"], ["log", "Logarithmic"]] }, { id: "A2", key: "flows", title: "The same flows, rebased", sub: "First available month = 100 · compare relative change", scale: "indexed", choices: [["indexed", "Rebased to 100"], ["linear", "Linear"], ["log", "Logarithmic"]] }] },
    companies: { description: "The same companies and month, with scale made explicit. Compare revenue or signed growth with a zero baseline; the table keeps the other metrics alongside it.", cells: [{ id: "B1", key: "revenues", title: "Company comparison", sub: "One common reporting month · choose the metric", scale: "revenue", choices: [["revenue", "Revenue"], ["mom", "MoM growth"], ["yoy", "YoY growth"]] }, { id: "B2", key: "companyTable", title: "The numbers behind the comparison", sub: "Revenue, MoM and YoY in one readable projection" }] },
    frontier: { description: "Compare three current model families each from OpenAI, Anthropic and Google. Each family uses the strongest evaluated variant of its latest release. Select a company to focus it; other providers remain available.", cells: [{ id: "C1", key: "frontier", title: "Model cost and quality", sub: "Output price vs recorded intelligence index", scale: "linear", choices: [["linear", "Linear price"], ["log", "Log price"]] }, { id: "C2", key: "frontier", title: "The same observations, log price", sub: "Equal distance represents equal price ratios", scale: "log", choices: [["log", "Log price"], ["linear", "Linear price"]] }] },
    mix: { description: "A part-to-whole example with a stated denominator. This is the monthly revenue mix of three selected memory companies, not their DRAM/NAND market share.", cells: [{ id: "D1", key: "mix", title: "Selected memory-company revenue mix", sub: "Nanya Technology, Winbond and Macronix", choices: [["donut", "Donut"], ["pie", "Pie"]], scale: "donut" }, { id: "D2", key: "memoryRevenue", title: "The same three companies, as bars", sub: "Absolute monthly revenue · NTD billions" }] },
    chain: { description: "A bounded projection of an existing supply-chain snapshot. The graph retains stored relationships; the list exposes which stage and node labels are actually present. It is a structural view, not a verified production-allocation model.", cells: [{ id: "E1", key: "chain", title: "HBM4 supplier and packaging chain", sub: "Stored relationships · full-width graph" }, { id: "E2", key: "chainTable", title: "Inspect the same nodes", sub: "Stage, entity and stored detail" }] }
  };
  function render() {
    if (!data) return;
    const started = performance.now(); const current = ++revision; mounted.forEach((m) => m.destroy()); mounted = []; grid.replaceChildren();
    const description = document.getElementById("review-description"); description.textContent = scenarios[scenario].description; grid.dataset.scenario = scenario;
    if (scenario === "frontier") {
      description.append(document.createTextNode(" For cost per task and reasoning variants, compare "));
      const reference = text("a", "", "Slopalytics ↗"); reference.href = "https://slopalytics.com/?chart=cost-task"; reference.target = "_blank"; reference.rel = "noopener noreferrer";
      description.append(reference, document.createTextNode(". Cost per task is a different measure from the output-token price shown here."));
    }
    scenarios[scenario].cells.forEach((c) => {
      const spec = data.cells[c.key]; const card = text("article", "review-card", ""); const head = text("header", "review-card-head", ""); const title = text("div", "", ""); const subtitle = text("p", "review-card-sub", c.sub); if (spec.period) subtitle.append(document.createTextNode(" · "), text("span", "review-atom", spec.period)); title.append(text("span", "review-case-id", c.id), text("h2", "", c.title), subtitle); head.append(title);
      let select;
      if (c.choices) { select = document.createElement("select"); select.setAttribute("aria-label", `${c.id} representation`); c.choices.forEach(([value, label]) => { const option = text("option", "", label); option.value = value; select.append(option); }); select.value = selections.get(c.id) || c.scale; head.append(select); }
      function describeRepresentation() {
        const scale = select?.value || c.scale; let label = c.sub;
        if (spec.type === "line") label = scale === "indexed" ? "First nonzero month = 100 · relative change" : `Monthly trade value · ${spec.unit}${scale === "log" ? " · log scale" : ""}`;
        if (c.id === "C2") { title.querySelector("h2").textContent = `The same observations, ${scale} price`; label = scale === "log" ? "Equal distance represents equal price ratios" : "Equal distance represents equal price differences"; }
        subtitle.replaceChildren(document.createTextNode(label)); if (spec.period) subtitle.append(document.createTextNode(" · "), text("span", "review-atom", spec.period));
      }
      describeRepresentation();
      const host = text("div", "review-cell", ""); host.dataset.cellId = c.id; const source = text("div", "review-card-source", spec.sourceLabel);
      card.append(head, host, source); grid.append(card);
      const opts = () => ({ scale: select?.value || c.scale || "linear", pie: select?.value === "pie", compact: document.getElementById("review-size").value === "compact", providerFocus, modelDetail,
        onProviderFocus: (creator) => { providerFocus = creator; render(); }, onModelDetail: (detail) => { modelDetail = detail; render(); } });
      const selectedSpec = () => c.id === "B1" && select.value !== "revenue" ? { ...spec, signed: true, unit: select.value === "mom" ? "Month-on-month change · %" : "Year-on-year change · %", rows: spec.rows.map((r) => ({ ...r, value: r[select.value] })) } : spec;
      const view = window.ThothResearchCells.mount(host, selectedSpec(), opts()); mounted.push(view);
      select?.addEventListener("change", () => { selections.set(c.id, select.value); describeRepresentation(); view.update(selectedSpec(), opts()); });
    });
    requestAnimationFrame(() => requestAnimationFrame(() => { if (current === revision) document.getElementById("review-status").textContent = `${scenario} · ${Math.round(performance.now() - started)} ms to render`; }));
  }
  document.querySelectorAll("[data-scenario]").forEach((button) => button.addEventListener("click", () => { scenario = button.dataset.scenario; document.querySelectorAll("[data-scenario]").forEach((b) => b.setAttribute("aria-pressed", String(b === button))); render(); }));
  document.getElementById("review-theme").addEventListener("change", (event) => { document.documentElement.dataset.theme = event.target.value; render(); });
  document.getElementById("review-size").addEventListener("change", render);
  document.getElementById("review-layout").addEventListener("change", (event) => { grid.dataset.layout = event.target.value; });
  window.addEventListener("pagehide", () => mounted.forEach((m) => m.destroy()));
  fetch("/data.json").then((r) => { if (!r.ok) throw Error("Snapshot unavailable"); return r.json(); }).then((payload) => { data = payload; document.getElementById("review-asof").textContent = `PREVIEW SNAPSHOT\n${data.capturedAt}`; render(); }).catch((err) => { grid.replaceChildren(text("p", "review-loading", err.message)); });
})();
