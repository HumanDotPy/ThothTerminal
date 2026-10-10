(function () {
  const root = document.querySelector("[data-memory-system]");
  if (!root) return;

  function themeColors() {
    const styles = getComputedStyle(root);
    const value = (name, fallback) => styles.getPropertyValue(name).trim() || fallback;
    return {
      amber: value("--memory-accent", "#ffffff"),
      green: value("--memory-good", "#79d68c"),
      red: value("--memory-bad", "#ff5d5d"),
      blue: value("--memory-accent-2", "#777777"),
      gray: value("--memory-muted", "#888888"),
      text: value("--thoth-mood-text", "#ffffff"),
      surface: "#050607",
      line: value("--memory-line", "rgba(255,255,255,.2)"),
    };
  }
  const chart = window.echarts ? window.echarts.init(root.querySelector("[data-chart]"), null, { renderer: "canvas" }) : null;
  const state = { view: "overview", scenario: "base", mode: "datasets", result: null, payload: null, selectedIndex: -1 };
  let requestSequence = 0;
  let debounceTimer = null;

  const defaults = {
    gross_wpm: 120000,
    equipped_fraction: 0.9,
    utilization: 0.92,
    ramp_factor: 0.85,
    node_beta_share: 0.58,
    node_gamma_share: 0.42,
    ai_system_units: 12000,
    hbm_gb_per_system: 192,
    server_system_units: 80000,
    ddr_gb_per_system: 512,
    hbm_asp: 9.5,
    ddr_asp: 2.1,
    capex: 180000000,
    invested_capital: 4200000000,
  };

  const viewMeta = {
    overview: ["System balance", "Supply and demand"],
    fab: ["Physical capacity", "Fab and process-node ledger"],
    conversion: ["Yield waterfall", "Qualified good-bit conversion"],
    demand: ["Architecture bridge", "System shipments and memory BOM"],
    allocation: ["Closed allocation", "Node capacity to memory products"],
    constraints: ["Bottleneck map", "Active output constraints"],
    economics: ["Supplier returns", "Revenue, cash flow, and ROIC"],
    assumptions: ["Model governance", "Assumptions and evidence lineage"],
    evidence: ["Sparse evidence", "Source observations and estimates"],
  };

  function number(value, fallback) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : fallback;
  }

  function inputValue(key) {
    const input = root.querySelector(`[data-input="${key}"]`);
    return number(input && input.value, defaults[key]);
  }

  function setInputs(values) {
    Object.entries(values).forEach(([key, value]) => {
      const input = root.querySelector(`[data-input="${key}"]`);
      if (input) input.value = value;
    });
  }

  function scenarioFactors() {
    if (state.scenario === "low") return { demand: 0.8, utilization: 0.9, asp: 0.88, yield: 0.96 };
    if (state.scenario === "high") return { demand: 1.25, utilization: 1.04, asp: 1.14, yield: 1.025 };
    return { demand: 1, utilization: 1, asp: 1, yield: 1 };
  }

  function buildPayload() {
    const factor = scenarioFactors();
    const betaShare = inputValue("node_beta_share");
    const gammaShare = inputValue("node_gamma_share");
    const capex = inputValue("capex");
    const investedCapital = inputValue("invested_capital");
    return {
      scenario_id: state.scenario,
      assumption_set_id: `memory-ui-${state.scenario}-2027`,
      period: "2027",
      period_basis: "monthly run-rate",
      capacity_pools: [{
        pool_id: "memoryco-fab-a",
        supplier: "MemoryCo",
        site: "Campus A",
        fab: "Fab A",
        gross_wpm: inputValue("gross_wpm"),
        equipped_fraction: inputValue("equipped_fraction"),
        eligible_fraction: 1,
        qualification_fraction: 1,
        utilization: Math.min(1, inputValue("utilization") * factor.utilization),
        ramp_factor: inputValue("ramp_factor"),
        lineage: "house_assumption",
        source_owner: "Thoth operator",
      }],
      node_allocations: [
        { parent_pool_id: "memoryco-fab-a", node_pool_id: "memoryco-fab-a-1beta", process_node: "1beta", node_allocation_share: betaShare, node_eligibility_status: "qualified", lineage: "house_assumption" },
        { parent_pool_id: "memoryco-fab-a", node_pool_id: "memoryco-fab-a-1gamma", process_node: "1gamma", node_allocation_share: gammaShare, node_eligibility_status: "ramping", lineage: "house_assumption" },
      ],
      product_conversions: [
        { pool_id: "memoryco-fab-a-1beta", product: "DDR5", memory_generation: "DDR5", dies_per_wafer: 600, die_capacity_gb: 2, die_yield: Math.min(1, 0.86 * factor.yield), core_dies_per_stack: 1, stack_yield: 1, package_test_yield: 0.98, lineage: "house_assumption" },
        { pool_id: "memoryco-fab-a-1gamma", product: "HBM4", memory_generation: "HBM4", dies_per_wafer: 420, die_capacity_gb: 3, die_yield: Math.min(1, 0.78 * factor.yield), core_dies_per_stack: 12, stack_yield: 0.9, package_test_yield: 0.95, base_die_capacity_stacks: 1800000, stacking_capacity_stacks: 1600000, package_test_capacity_stacks: 1500000, customer_qualified_capacity_stacks: 1400000, lineage: "house_assumption" },
      ],
      allocations: [
        { pool_id: "memoryco-fab-a-1beta", product: "DDR5", allocation_share: 1, lineage: "house_assumption" },
        { pool_id: "memoryco-fab-a-1gamma", product: "HBM4", allocation_share: 1, lineage: "house_assumption" },
      ],
      system_shipments: [
        { platform: "AI accelerator system", period: "2027", period_months: 12, system_units: Math.round(inputValue("ai_system_units") * factor.demand), lineage: "analyst_estimate", estimate_owner: "Scenario input" },
        { platform: "Enterprise server", period: "2027", period_months: 12, system_units: Math.round(inputValue("server_system_units") * factor.demand), lineage: "analyst_estimate", estimate_owner: "Scenario input" },
      ],
      memory_bom: [
        { platform: "AI accelerator system", period: "2027", product: "HBM4", memory_gb_per_system: inputValue("hbm_gb_per_system"), lineage: "house_assumption" },
        { platform: "Enterprise server", period: "2027", product: "DDR5", memory_gb_per_system: inputValue("ddr_gb_per_system"), lineage: "house_assumption" },
      ],
      economics: [
        { pool_id: "memoryco-fab-a-1gamma", supplier: "MemoryCo", product: "HBM4", asp_per_gb: inputValue("hbm_asp") * factor.asp, wafer_cost: 7200, stack_package_test_cost: 42, variable_cost_per_gb: 1.8, fixed_cost: 24000000, opex: 18000000, depreciation: 32000000, capex: capex * 0.65 / 12, cash_taxes: 8000000, working_capital_change: 6000000, invested_capital: investedCapital * 0.65, target_gross_margin: 0.55, tax_rate: 0.18, lineage: "house_assumption" },
        { pool_id: "memoryco-fab-a-1beta", supplier: "MemoryCo", product: "DDR5", asp_per_gb: inputValue("ddr_asp") * factor.asp, wafer_cost: 6100, stack_package_test_cost: 0.8, variable_cost_per_gb: 0.55, fixed_cost: 17000000, opex: 11000000, depreciation: 21000000, capex: capex * 0.35 / 12, cash_taxes: 5000000, working_capital_change: 4000000, invested_capital: investedCapital * 0.35, target_gross_margin: 0.4, tax_rate: 0.18, lineage: "house_assumption" },
      ],
    };
  }

  function compact(value, digits) {
    const n = Number(value);
    if (!Number.isFinite(n)) return "—";
    const abs = Math.abs(n);
    const units = [[1e12, "T"], [1e9, "B"], [1e6, "M"], [1e3, "K"]];
    for (const [divisor, suffix] of units) if (abs >= divisor) return `${(n / divisor).toFixed(digits == null ? 1 : digits).replace(/\.0$/, "")}${suffix}`;
    return n.toLocaleString(undefined, { maximumFractionDigits: digits == null ? 1 : digits });
  }

  function percent(value) {
    const n = Number(value);
    return Number.isFinite(n) ? `${(n * 100).toFixed(1)}%` : "—";
  }

  function money(value) { return `$${compact(value)}`; }
  function escapeHtml(value) {
    return String(value == null ? "" : value).replace(/[&<>"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[char]));
  }

  function tableModel(view, result) {
    if (view === "evidence") return {
      columns: [["metric", "Observation"], ["period_label", "Period"], ["value", "Value"], ["unit", "Unit"], ["source_owner", "Source"], ["source_document_id", "Document"]],
      rows: result.source_evidence || [],
    };
    if (view === "fab") return {
      columns: [["pool_kind", "Level"], ["pool_id", "Pool"], ["process_node", "Node"], ["gross", "Gross WPM", "number"], ["equipped", "Equipped", "number"], ["qualified", "Qualified", "number"], ["utilized", "Utilized", "number"]],
      rows: result.capacity_pools.map((pool) => ({ ...pool, ...pool.capacity_states_wpm })),
    };
    if (view === "conversion") return {
      columns: [["product", "Product"], ["process_node", "Node"], ["allocated_wpm", "Allocated WPM", "number"], ["gross_dies", "Gross dies", "number"], ["good_memory_dies", "Good dies", "number"], ["qualified_shipped_gb", "Qualified GB", "number"], ["active_constraint", "Binding stage"]],
      rows: result.allocations,
    };
    if (view === "demand") return {
      columns: [["system_or_platform", "Platform"], ["product", "Product"], ["period", "Period"], ["system_units", "Systems / month", "number"], ["raw_system_units", "Systems / period", "number"], ["period_months", "Months", "number"], ["memory_gb_per_system", "GB / system", "number"], ["demand_gb", "Demand GB / month", "number"]],
      rows: result.demand,
    };
    if (view === "allocation") return {
      columns: [["pool_id", "Node pool"], ["product", "Product"], ["allocated_wpm", "WPM", "number"], ["allocation_share", "Pool share", "percent"], ["qualified_shipped_gb", "Shipped GB", "number"], ["active_constraint", "Constraint"]],
      rows: result.allocations,
    };
    if (view === "constraints") return {
      columns: [["product", "Product"], ["pool_id", "Pool"], ["active_constraint", "Active constraint"], ["front_end", "Front end", "number"], ["base_die", "Base die", "number"], ["stacking_bonding", "Stacking", "number"], ["package_test", "Package/test", "number"], ["customer_qualification", "Qualified", "number"]],
      rows: result.allocations.map((row) => ({ ...row, front_end: row.stage_output_stacks.front_end_yielded, ...row.stage_output_stacks })),
    };
    if (view === "economics") return {
      columns: [["supplier", "Supplier"], ["product", "Product"], ["period", "Period"], ["shipped_revenue", "Shipped revenue", "money"], ["notional_unmet_revenue", "Unmet notional", "money"], ["gross_margin", "Gross margin", "percent"], ["free_cash_flow", "Free cash flow", "money"], ["roic", "ROIC", "percent"], ["capacity_clearing_asp_per_gb", "Clearing ASP / GB", "money"]],
      rows: result.economics,
    };
    if (view === "assumptions") {
      const rows = [];
      result.capacity_pools.forEach((row) => rows.push({ section: "Capacity", object: row.pool_id, lineage: row.lineage && row.lineage.lineage, owner: row.lineage && row.lineage.source_owner, status: row.stage_basis && row.stage_basis.utilized }));
      result.product_conversions.forEach((row) => rows.push({ section: "Conversion", object: `${row.pool_id} / ${row.product}`, lineage: row.lineage && row.lineage.lineage, owner: row.lineage && row.lineage.estimate_owner, status: "active" }));
      result.demand.forEach((row) => rows.push({ section: "Demand", object: `${row.system_or_platform} / ${row.product}`, lineage: row.lineage && row.lineage.lineage, owner: row.lineage && row.lineage.estimate_owner, status: row.period }));
      result.economics.forEach((row) => rows.push({ section: "Economics", object: `${row.supplier} / ${row.product}`, lineage: row.lineage && row.lineage.lineage, owner: row.lineage && row.lineage.estimate_owner, status: row.period || "scenario" }));
      return { columns: [["section", "Section"], ["object", "Object"], ["lineage", "Lineage"], ["owner", "Source / owner"], ["status", "Basis"]], rows };
    }
    return {
      columns: [["product", "Product"], ["period", "Period"], ["supply_gb", "Supply GB / month", "number"], ["demand_gb", "Demand GB / month", "number"], ["surplus_deficit_gb", "Surplus / deficit / month", "signed"], ["demand_fulfillment", "Fulfillment", "percent"]],
      rows: result.product_balances,
    };
  }

  function cellValue(value, type) {
    if (type === "percent") return percent(value);
    if (type === "money") return money(value);
    if (type === "number" || type === "signed") return compact(value);
    return value == null || value === "" ? "—" : String(value);
  }

  function renderTable() {
    if (!state.result) return;
    const model = tableModel(state.view, state.result);
    const head = root.querySelector("[data-table-head]");
    const body = root.querySelector("[data-table-body]");
    head.innerHTML = `<tr>${model.columns.map((column) => `<th>${escapeHtml(column[1])}</th>`).join("")}</tr>`;
    body.innerHTML = model.rows.map((row, index) => `<tr data-row-index="${index}" class="${index === state.selectedIndex ? "selected" : ""}">${model.columns.map((column) => {
      const value = row[column[0]];
      const classes = [column[2] && column[2] !== "percent" ? "number" : ""];
      if (column[2] === "signed") classes.push(Number(value) < 0 ? "bad" : "good");
      return `<td class="${classes.join(" ")}" title="${escapeHtml(cellValue(value, column[2]))}">${escapeHtml(cellValue(value, column[2]))}</td>`;
    }).join("")}</tr>`).join("");
    root.querySelector("[data-empty]").hidden = model.rows.length > 0;
    root.querySelector("[data-row-count]").textContent = `${model.rows.length} rows${state.view === "evidence" && state.result.source_evidence_truncated ? " (truncated)" : ""}`;
    body.querySelectorAll("tr").forEach((row) => row.addEventListener("click", () => selectRow(model.rows[Number(row.dataset.rowIndex)], Number(row.dataset.rowIndex))));
  }

  function selectRow(row, index) {
    state.selectedIndex = index;
    const detail = root.querySelector("[data-selected-detail]");
    const entries = Object.entries(row).filter(([, value]) => value != null && value !== "" && typeof value !== "object").slice(0, state.view === "evidence" ? 100 : 12);
    detail.innerHTML = entries.map(([key, value]) => `<dt>${escapeHtml(key.replaceAll("_", " "))}</dt><dd>${escapeHtml(cellValue(value, typeof value === "number" ? "number" : ""))}</dd>`).join("");
    renderTable();
  }

  function commonChartOption() {
    const colors = themeColors();
    return {
      animationDuration: 260,
      backgroundColor: "transparent",
      textStyle: { color: colors.text, fontFamily: '"OMORI GAME", Consolas, monospace', fontSize: 11 },
      grid: { left: 62, right: 24, top: 42, bottom: 42 },
      tooltip: { trigger: "axis", backgroundColor: colors.surface, borderColor: colors.line, textStyle: { color: colors.text, fontSize: 10 } },
      legend: { top: 12, right: 18, textStyle: { color: colors.gray, fontSize: 10 } },
      xAxis: { type: "category", axisLine: { lineStyle: { color: colors.line } }, axisLabel: { color: colors.gray }, axisTick: { show: false } },
      yAxis: { type: "value", splitLine: { lineStyle: { color: colors.line, type: "dashed" } }, axisLabel: { color: colors.gray, formatter: (value) => compact(value, 0) } },
    };
  }

  function renderChart() {
    if (!chart || !state.result) return;
    root.querySelector('[data-chart]').hidden = state.view === 'evidence';
    const result = state.result;
    const COLORS = themeColors();
    const option = commonChartOption();
    if (state.view === "evidence") {
      chart.clear();
      chart.setOption({title: {text: 'Source claims remain separate from modeled output', left: 'center', top: 'center', textStyle: {fontSize: 12, color: COLORS.text}}});
      return;
    }
    if (state.view === "overview") {
      option.xAxis.data = result.product_balances.map((row) => `${row.product} · ${row.period || "unspecified"}`);
      option.series = [
        { name: "Supply GB", type: "bar", data: result.product_balances.map((row) => row.supply_gb), itemStyle: { color: COLORS.green } },
        { name: "Demand GB", type: "bar", data: result.product_balances.map((row) => row.demand_gb), itemStyle: { color: COLORS.amber } },
      ];
    } else if (state.view === "fab") {
      option.xAxis.data = result.capacity_pools.map((row) => row.process_node || "Physical fab");
      option.series = ["gross", "equipped", "qualified", "utilized"].map((key, index) => ({ name: key, type: "bar", data: result.capacity_pools.map((row) => row.capacity_states_wpm[key]), itemStyle: { color: [COLORS.gray, COLORS.blue, COLORS.amber, COLORS.green][index] } }));
    } else if (state.view === "conversion" || state.view === "allocation") {
      option.xAxis.data = result.allocations.map((row) => `${row.process_node || "node"} / ${row.product}`);
      option.series = [
        { name: "Allocated WPM", type: "bar", yAxisIndex: 0, data: result.allocations.map((row) => row.allocated_wpm), itemStyle: { color: COLORS.blue } },
        { name: "Qualified GB", type: "line", yAxisIndex: 1, data: result.allocations.map((row) => row.qualified_shipped_gb), itemStyle: { color: COLORS.green }, lineStyle: { color: COLORS.green } },
      ];
      option.yAxis = [option.yAxis, { type: "value", splitLine: { show: false }, axisLabel: { color: COLORS.gray, formatter: (value) => compact(value, 0) } }];
    } else if (state.view === "demand") {
      option.xAxis.data = result.demand.map((row) => `${row.system_or_platform} · ${row.period || "unspecified"}`);
      option.series = [{ name: "Demand GB", type: "bar", data: result.demand.map((row) => row.demand_gb), itemStyle: { color: COLORS.amber } }];
    } else if (state.view === "constraints") {
      option.xAxis.data = result.allocations.map((row) => row.product);
      const keys = [["front_end_yielded", "Front end"], ["base_die", "Base die"], ["stacking_bonding", "Stacking"], ["package_test", "Package/test"], ["customer_qualification", "Qualified"]];
      option.series = keys.map(([key, label], index) => ({ name: label, type: "bar", data: result.allocations.map((row) => row.stage_output_stacks[key] == null ? null : row.stage_output_stacks[key]), itemStyle: { color: [COLORS.blue, COLORS.gray, COLORS.amber, COLORS.green, "#9d7cb8"][index] } }));
    } else if (state.view === "economics") {
      option.xAxis.data = result.economics.map((row) => `${row.product} · ${row.period || "unspecified"}`);
      option.series = [
        { name: "Revenue", type: "bar", data: result.economics.map((row) => row.shipped_revenue), itemStyle: { color: COLORS.blue } },
        { name: "Gross profit", type: "bar", data: result.economics.map((row) => row.gross_profit), itemStyle: { color: COLORS.green } },
        { name: "Free cash flow", type: "bar", data: result.economics.map((row) => row.free_cash_flow), itemStyle: { color: COLORS.amber } },
      ];
    } else {
      const audit = result.audit;
      option.xAxis.data = ["Errors", "Warnings", "Defaults", "Derived", "Missing evidence"];
      option.series = [{ name: "Audit count", type: "bar", data: [audit.errors.length, audit.warnings.length, Object.keys(audit.defaults_used).length, audit.derived_fields.length, audit.missing_evidence.length], itemStyle: { color: (params) => [COLORS.red, COLORS.amber, COLORS.gray, COLORS.blue, "#9d7cb8"][params.dataIndex] } }];
    }
    chart.setOption(option, true);
  }

  function renderKpis() {
    const result = state.result;
    if (!result.audit.scenario_valid || new Set(result.product_balances.map(row => row.period)).size > 1) {
      root.querySelectorAll('[data-kpi]').forEach(node => { node.textContent = "—"; node.classList.remove("positive", "negative"); });
      root.querySelector('[data-kpi-note="balance"]').textContent = "Resolve gaps / select one period";
      return;
    }
    const parent = result.capacity_pools.find((pool) => pool.pool_kind === "physical_parent") || result.capacity_pools[0];
    const supply = result.product_balances.reduce((sum, row) => sum + Number(row.supply_gb || 0), 0);
    const demand = result.product_balances.reduce((sum, row) => sum + Number(row.demand_gb || 0), 0);
    const balance = supply - demand;
    const revenue = result.economics.reduce((sum, row) => sum + Number(row.shipped_revenue || 0), 0);
    const capital = result.economics.reduce((sum, row) => sum + Number(row.invested_capital || 0), 0);
    const nopat = result.economics.reduce((sum, row) => sum + Number(row.nopat || 0), 0);
    const values = { wpm: compact(parent && parent.capacity_states_wpm.utilized), supply: compact(supply), demand: compact(demand), balance: `${balance >= 0 ? "+" : ""}${compact(balance)}`, revenue: money(revenue), roic: capital > 0 ? percent(nopat / capital) : "—" };
    Object.entries(values).forEach(([key, value]) => { root.querySelector(`[data-kpi="${key}"]`).textContent = value; });
    const balanceNode = root.querySelector('[data-kpi="balance"]');
    balanceNode.classList.toggle("negative", balance < 0); balanceNode.classList.toggle("positive", balance >= 0);
    root.querySelector('[data-kpi-note="balance"]').textContent = balance < 0 ? "Unmet demand" : "Available surplus";
  }

  function renderAudit() {
    const audit = state.result.audit;
    root.querySelector("[data-completeness]").textContent = audit.completeness;
    root.querySelector('[data-audit-count="errors"]').textContent = audit.errors.length;
    root.querySelector('[data-audit-count="warnings"]').textContent = audit.warnings.length;
    root.querySelector('[data-audit-count="defaults"]').textContent = Object.keys(audit.defaults_used).length;
    const constraints = state.result.allocations.map((row) => `<div><strong>${escapeHtml(row.product)} · ${escapeHtml(row.active_constraint)}</strong>${escapeHtml(row.process_node || row.pool_id)}</div>`).join("");
    root.querySelector("[data-constraint-list]").innerHTML = constraints || "<div>No allocation constraints.</div>";
    const findings = [
      ...audit.errors.map((text) => ["bad", "Error", text]),
      ...audit.warnings.map((text) => ["warn", "Warning", text]),
      ...audit.missing_evidence.slice(0, 8).map((text) => ["", "Missing evidence", text]),
    ];
    root.querySelector("[data-audit-list]").innerHTML = findings.length ? findings.map(([kind, label, text]) => `<div class="${kind}"><strong>${label}</strong>${escapeHtml(text)}</div>`).join("") : "<div><strong>No blocking findings</strong>Scenario equations close.</div>";
  }

  function render() {
    if (!state.result) return;
    const valid = Boolean(state.result.audit.scenario_valid);
    root.querySelector("[data-status]").textContent = valid ? "Scenario valid" : (state.mode === "datasets" ? "Evidence incomplete — inspect available rows" : "Scenario invalid");
    root.querySelector("[data-status-dot]").className = `memory-status-dot ${valid ? "valid" : "invalid"}`;
    root.querySelector("[data-model-version]").textContent = state.result.model_version;
    const period = state.result.scenario.period || "Period unspecified";
    root.querySelector("[data-period]").textContent = state.result.scenario.period_basis ? `${period} · ${state.result.scenario.period_basis}` : period;
    const hasDraft = ((state.result.dataset_resolution || {}).datasets || []).some(row => row.status === 'draft');
    root.querySelector("[data-freshness]").textContent = `${state.mode === "datasets" ? (hasDraft ? "Includes draft evidence" : "Published evidence") : "Demonstration"} · ${state.scenario}`;
    root.querySelector("[data-view-kicker]").textContent = viewMeta[state.view][0];
    root.querySelector("[data-view-title]").textContent = viewMeta[state.view][1];
    renderKpis(); renderAudit(); renderTable(); renderChart();
  }

  async function calculate() {
    const sequence = ++requestSequence;
    const mode = state.mode;
    let requestPayload;
    if (mode === "datasets") {
      const dataset_ids = {};
      root.querySelectorAll('[data-dataset-family]').forEach(select => { if (select.value) dataset_ids[select.dataset.datasetFamily] = [Number(select.value)]; });
      requestPayload = {scenario_id: state.scenario, period: root.querySelector('[data-source-period]').value.trim() || null, dataset_ids, allow_draft: root.querySelector('[data-allow-draft]').checked};
    } else { requestPayload = buildPayload(); }
    state.result = null;
    state.payload = null;
    root.querySelector("[data-status]").textContent = "Calculating";
    root.querySelector("[data-status-dot]").className = "memory-status-dot";
    try {
      const response = await fetch(mode === "datasets" ? "/api/memory/system/evaluate" : "/api/memory/system/preview", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(requestPayload) });
      const body = await response.json();
      if (sequence !== requestSequence) return;
      if (!response.ok || !body.ok) throw new Error(body.error || "Calculation failed");
      state.result = body.result;
      state.payload = mode === "datasets" ? body.result.resolved_input : requestPayload;
      const sources = (body.result.dataset_resolution || {}).datasets || [];
      root.querySelector('[data-source-provenance]').innerHTML = sources.map(row => `<p><a href="/visualizations/datasets/${Number(row.dataset_id)}">${escapeHtml(row.slug)}</a> · version ${escapeHtml(row.version_id || row.dataset_version_id)} · ${escapeHtml(row.status)}</p>`).join('') || (mode === "datasets" ? "No input versions resolved." : "");
      state.selectedIndex = -1;
      render();
    } catch (error) {
      if (sequence !== requestSequence) return;
      root.querySelector("[data-status]").textContent = error.message || "Calculation failed";
      root.querySelector("[data-status-dot]").className = "memory-status-dot invalid";
    }
  }

  function scheduleCalculate() { clearTimeout(debounceTimer); debounceTimer = setTimeout(calculate, 220); }
  function exportResult() {
    if (!state.result) return;
    const blob = new Blob([JSON.stringify({ model: state.payload, result: state.result }, null, 2)], { type: "application/json" });
    const link = document.createElement("a"); link.href = URL.createObjectURL(blob); link.download = `memory-system-${state.scenario}.json`; link.click(); URL.revokeObjectURL(link.href);
  }
  async function materialize() {
    if (!state.result || !state.payload || !state.result.audit.scenario_valid) return window.alert("Only a valid scenario can be materialized.");
    if (!window.confirm(`Materialize the ${state.scenario} scenario as a versioned Memory System output dataset?`)) return;
    const source_dataset_ids = ((state.result.dataset_resolution || {}).datasets || []).map(row => row.dataset_id);
    const response = await fetch("/api/memory/system/materialize", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ model: state.payload, source_dataset_ids, slug: `memory-system-${state.mode}-${state.scenario}-output`, publish: false }) });
    const body = await response.json();
    if (!response.ok || !body.ok) return window.alert(body.error || "Materialization failed.");
    window.alert(`Materialized draft dataset ${body.materialization.dataset_id}.`);
  }

  root.querySelectorAll("[data-view]").forEach((button) => button.addEventListener("click", () => {
    state.view = button.dataset.view; state.selectedIndex = -1;
    root.querySelectorAll("[data-view]").forEach((item) => item.classList.toggle("active", item === button)); render();
  }));
  root.querySelectorAll("[data-scenario]").forEach((button) => button.addEventListener("click", () => {
    state.scenario = button.dataset.scenario;
    root.querySelectorAll("[data-scenario]").forEach((item) => item.classList.toggle("active", item === button)); calculate();
  }));
  root.querySelectorAll("[data-input]").forEach((input) => input.addEventListener("input", scheduleCalculate));
  root.querySelector('[data-action="calculate"]').addEventListener("click", calculate);
  root.querySelector('[data-action="reset"]').addEventListener("click", () => { setInputs(defaults); state.scenario = "base"; root.querySelectorAll("[data-scenario]").forEach((item) => item.classList.toggle("active", item.dataset.scenario === "base")); calculate(); });
  root.querySelector('[data-action="export"]').addEventListener("click", exportResult);
  root.querySelector('[data-action="materialize"]').addEventListener("click", materialize);
  function applySourceMode() {
    state.mode = root.querySelector('[data-source-mode]').value;
    const demo = state.mode === "demo";
    root.querySelector('[data-input-form]').hidden = !demo;
    root.querySelector('[data-demo-notice]').hidden = !demo;
    root.querySelector('[data-dataset-controls]').hidden = demo;
    calculate();
  }
  root.querySelector('[data-source-mode]').addEventListener('change', applySourceMode);
  root.querySelector('[data-source-period]').addEventListener('change', calculate);
  root.querySelector('[data-allow-draft]').addEventListener('change', calculate);
  async function loadInputs() {
    root.querySelector('[data-input-form]').hidden = true;
    try {
      const response = await fetch('/api/memory/system/inputs');
      const body = await response.json();
      if (!response.ok || !body.ok) throw new Error(body.error || 'Dataset catalog unavailable');
      const families = body.families || [...new Set(body.datasets.map(row => row.family))];
      root.querySelector('[data-dataset-selectors]').innerHTML = families.map(family => {
        const rows = body.datasets.filter(row => row.family === family);
        return `<label>${escapeHtml(family.replaceAll('_', ' '))}<select data-dataset-family="${escapeHtml(family)}"><option value="">${rows.length ? 'Automatic when unambiguous' : 'No active datasets'}</option>${rows.map(row => `<option value="${Number(row.id)}">#${Number(row.id)} ${escapeHtml(row.name || row.slug)}${row.latest_published_version_id ? '' : ' (unpublished)'}</option>`).join('')}</select></label>`;
      }).join('') + (body.truncated ? '<p>Catalog truncated. Open the dataset workbench to narrow sources.</p>' : '');
      root.querySelectorAll('[data-dataset-family]').forEach(select => select.addEventListener('change', calculate));
      calculate();
    } catch (error) {
      root.querySelector('[data-dataset-selectors]').textContent = error.message;
      root.querySelector('[data-status]').textContent = 'Dataset catalog unavailable';
    }
  }
  window.addEventListener("resize", () => chart && chart.resize());
  if (chart && "ResizeObserver" in window) {
    const chartElement = root.querySelector("[data-chart]");
    root._memoryChartResizeObserver = new ResizeObserver(() => {
      requestAnimationFrame(() => chart.resize({ width: chartElement.clientWidth, height: chartElement.clientHeight }));
    });
    root._memoryChartResizeObserver.observe(chartElement);
  }
  window.addEventListener("thoth:moodchange", () => renderChart());
  setInputs(defaults);
  loadInputs();
})();
