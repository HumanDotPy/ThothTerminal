(function () {
  const root = document.querySelector("[data-robotics-model]");
  if (!root) return;

  const storageKeys = {
    robots: "thoth_robotics_working_robot_catalogue",
    tasks: "thoth_robotics_working_task_catalogue",
  };

  const seedRobots = [
    { id: "unitree-g1", oem: "Unitree", platform: "G1", type: "Humanoid", asp: 35000, useful_life_years: 2, source: "Working assumption" },
    { id: "agility-digit", oem: "Agility Robotics", platform: "Digit", type: "Humanoid", asp: 250000, useful_life_years: 4, source: "Working assumption" },
    { id: "figure-02", oem: "Figure", platform: "Figure 02", type: "Humanoid", asp: 150000, useful_life_years: 4, source: "Working assumption" },
    { id: "tesla-optimus", oem: "Tesla", platform: "Optimus", type: "Humanoid", asp: 30000, useful_life_years: 3, source: "Target-price scenario" },
    { id: "boston-dynamics-stretch", oem: "Boston Dynamics", platform: "Stretch", type: "Warehouse unloading", asp: 200000, useful_life_years: 5, source: "Working assumption" },
  ];

  const regionalPresets = [
    {
      id: "us-private-worker-2026",
      label: "United States private worker",
      region_name: "United States",
      currency: "USD",
      benefits_uplift_rate: 0.43,
      source: "BLS ECEC March 2026: private wages $32.60/hr, benefits $14.01/hr",
      confidence: 0.75,
    },
    {
      id: "eu-average-2025",
      label: "EU average loaded labor",
      region_name: "European Union",
      currency: "EUR",
      loaded_labor_cost_per_hour: 34.9,
      source: "Eurostat 2025 whole-economy hourly labour cost",
      confidence: 0.85,
    },
    {
      id: "germany-2025",
      label: "Germany loaded labor",
      region_name: "Germany",
      currency: "EUR",
      loaded_labor_cost_per_hour: 45.0,
      source: "Destatis/Eurostat 2025 business-economy labour cost per hour",
      confidence: 0.85,
    },
    {
      id: "poland-2025",
      label: "Poland loaded labor",
      region_name: "Poland",
      currency: "EUR",
      loaded_labor_cost_per_hour: 19.1,
      source: "Destatis/Eurostat 2025 business-economy labour cost per hour",
      confidence: 0.85,
    },
    {
      id: "japan-oecd-2024",
      label: "Japan average wage proxy",
      region_name: "Japan",
      currency: "USD_PPP",
      wage_per_hour: 23.77,
      source: "OECD average annual wages 2024 PPP, divided by 2,080 hours",
      confidence: 0.45,
    },
    {
      id: "korea-oecd-2024",
      label: "Korea average wage proxy",
      region_name: "Korea",
      currency: "USD_PPP",
      wage_per_hour: 21.88,
      source: "OECD average annual wages 2024 PPP, divided by 2,080 hours",
      confidence: 0.45,
    },
    {
      id: "china-nonprivate-2025",
      label: "China urban non-private average",
      region_name: "China",
      currency: "CNY",
      wage_per_hour: 62.23,
      benefits_uplift_rate: 0.28,
      source: "NBS 2025 urban non-private average annual wage RMB 129,441; employer social security varies locally, broad uplift proxy 28%",
      confidence: 0.55,
    },
    {
      id: "china-private-2025",
      label: "China urban private average",
      region_name: "China",
      currency: "CNY",
      wage_per_hour: 34.42,
      benefits_uplift_rate: 0.28,
      source: "NBS 2025 urban private average annual wage RMB 71,590; employer social security varies locally, broad uplift proxy 28%",
      confidence: 0.55,
    },
    {
      id: "china-manufacturing-2025",
      label: "China manufacturing wage proxy",
      region_name: "China",
      currency: "CNY",
      wage_per_hour: 54.61,
      benefits_uplift_rate: 0.28,
      source: "NBS-reported manufacturing wage proxy RMB 113,594/year, divided by 2,080 hours; broad employer contribution proxy 28%",
      confidence: 0.45,
    },
  ];

  const seedTasks = [
    {
      id: "warehouse-material-handling-us",
      domain: "Warehouse",
      task: "Material handling",
      role: "Laborers and freight, stock, and material movers",
      region_name: "United States",
      wage_per_hour: 19.35,
      benefits_uplift_rate: 0.25,
      loaded_labor_cost_per_hour: 24,
      utilization: 0.67,
      productivity_vs_human: 1,
      task_demand_coverage: 0.85,
      task_share_of_role: 0.7,
      intervention_minutes_per_robot_hour: 5,
      source: "O*NET/BLS 2025 median wage $19.35/hr; loaded-labor uplift assumption 25%",
      estimated_fields: ["loaded_labor_cost_per_hour", "task_demand_coverage", "intervention_minutes_per_robot_hour"],
    },
    {
      id: "truck-unloading-us",
      domain: "Warehouse",
      task: "Truck unloading",
      role: "Laborers and freight, stock, and material movers",
      region_name: "United States",
      wage_per_hour: 19.35,
      benefits_uplift_rate: 0.25,
      loaded_labor_cost_per_hour: 24,
      utilization: 0.45,
      productivity_vs_human: 1,
      task_demand_coverage: 0.35,
      task_share_of_role: 0.35,
      intervention_minutes_per_robot_hour: 8,
      source: "O*NET/BLS 2025 median wage $19.35/hr; lumpy dock-door task demand separated from continuous conveyor handling",
      estimated_fields: ["loaded_labor_cost_per_hour", "task_demand_coverage", "task_share_of_role", "intervention_minutes_per_robot_hour"],
    },
    {
      id: "retail-warehouse-order-filling-us",
      domain: "Warehouse / Retail",
      task: "Stocking and order filling",
      role: "Stockers and order fillers",
      region_name: "United States",
      wage_per_hour: 17.95,
      benefits_uplift_rate: 0.25,
      loaded_labor_cost_per_hour: 22.5,
      utilization: 0.6,
      productivity_vs_human: 1,
      task_demand_coverage: 0.65,
      task_share_of_role: 0.6,
      intervention_minutes_per_robot_hour: 6,
      source: "O*NET/BLS 2025 median wage $17.95/hr; loaded-labor uplift assumption 25%",
      estimated_fields: ["loaded_labor_cost_per_hour", "task_demand_coverage", "intervention_minutes_per_robot_hour"],
    },
    {
      id: "commercial-floor-cleaning-us",
      domain: "Facilities",
      task: "Commercial floor cleaning",
      role: "Janitors and cleaners",
      region_name: "United States",
      wage_per_hour: 17.71,
      benefits_uplift_rate: 0.25,
      loaded_labor_cost_per_hour: 22,
      utilization: 0.5,
      productivity_vs_human: 0.9,
      task_demand_coverage: 0.55,
      task_share_of_role: 0.65,
      intervention_minutes_per_robot_hour: 4,
      source: "O*NET/BLS 2025 median wage $17.71/hr; IFR identifies professional floor-cleaning robots as a scaled service-robot category",
      estimated_fields: ["loaded_labor_cost_per_hour", "task_demand_coverage", "productivity_vs_human", "intervention_minutes_per_robot_hour"],
    },
    {
      id: "hotel-housekeeping-us",
      domain: "Hospitality",
      task: "Room and hallway cleaning",
      role: "Maids and housekeeping cleaners",
      region_name: "United States",
      wage_per_hour: 17.07,
      benefits_uplift_rate: 0.25,
      loaded_labor_cost_per_hour: 21.5,
      utilization: 0.45,
      productivity_vs_human: 0.7,
      task_demand_coverage: 0.5,
      task_share_of_role: 0.45,
      intervention_minutes_per_robot_hour: 10,
      source: "O*NET/BLS 2025 median wage $17.07/hr; partial automation only because room reset requires dexterity and exception handling",
      estimated_fields: ["loaded_labor_cost_per_hour", "task_demand_coverage", "productivity_vs_human", "intervention_minutes_per_robot_hour"],
    },
    {
      id: "fast-food-counter-prep-us",
      domain: "Food service",
      task: "Counter service and simple prep",
      role: "Fast food and counter workers",
      region_name: "United States",
      wage_per_hour: 15,
      benefits_uplift_rate: 0.25,
      loaded_labor_cost_per_hour: 19,
      utilization: 0.55,
      productivity_vs_human: 0.8,
      task_demand_coverage: 0.6,
      task_share_of_role: 0.4,
      intervention_minutes_per_robot_hour: 8,
      source: "O*NET/BLS 2025 median wage $15.00/hr; robotics/software automation is workflow-specific rather than full-role replacement",
      estimated_fields: ["loaded_labor_cost_per_hour", "task_demand_coverage", "task_share_of_role", "productivity_vs_human", "intervention_minutes_per_robot_hour"],
    },
    {
      id: "factory-lineside-us",
      domain: "Factory",
      task: "Line-side material movement",
      role: "Material handler",
      region_name: "United States",
      loaded_labor_cost_per_hour: 38,
      utilization: 0.75,
      productivity_vs_human: 1,
      task_demand_coverage: 0.8,
      task_share_of_role: 0.7,
      intervention_minutes_per_robot_hour: 4,
      source: "Working assumption; needs NAICS/SOC split for manufacturing material handlers",
      estimated_fields: ["loaded_labor_cost_per_hour"],
    },
    {
      id: "security-patrol-us",
      domain: "Security",
      task: "Indoor patrol",
      role: "Security guard",
      region_name: "United States",
      loaded_labor_cost_per_hour: 28,
      utilization: 0.6,
      productivity_vs_human: 0.8,
      task_demand_coverage: 0.7,
      task_share_of_role: 0.35,
      intervention_minutes_per_robot_hour: 8,
      source: "Working assumption; lower priority because autonomy replaces patrol coverage, not full guard function",
      estimated_fields: ["loaded_labor_cost_per_hour", "productivity_vs_human"],
    },
    {
      id: "hospital-linen-us",
      domain: "Hospital",
      task: "Linen and supply transport",
      role: "Support staff",
      region_name: "United States",
      loaded_labor_cost_per_hour: 34,
      utilization: 0.55,
      productivity_vs_human: 0.75,
      task_demand_coverage: 0.6,
      task_share_of_role: 0.5,
      intervention_minutes_per_robot_hour: 6,
      source: "Working assumption; likely map to hospital orderlies/material movers in next data pass",
      estimated_fields: ["loaded_labor_cost_per_hour", "productivity_vs_human"],
    },
  ];

  const form = root.querySelector("#robotics-model-form");
  const regionPresetSelect = root.querySelector("[data-region-preset]");
  const matrixTable = root.querySelector("[data-matrix]");
  const statusPanel = root.querySelector("[data-status-panel]");
  const auditSummary = root.querySelector("[data-audit-summary]");
  const auditPanel = root.querySelector("[data-audit]");
  const gapList = root.querySelector("[data-gap-list]");
  const matrixControls = Array.from(root.querySelectorAll("[data-matrix-control]"));
  const outputNodes = new Map(Array.from(root.querySelectorAll("[data-output]")).map((node) => [node.dataset.output, node]));
  const solveNodes = new Map(Array.from(root.querySelectorAll("[data-solve]")).map((node) => [node.dataset.solve, node]));
  const selectedNodes = new Map(Array.from(root.querySelectorAll("[data-selected]")).map((node) => [node.dataset.selected, node]));
  const selectedTitle = root.querySelector("[data-selected-title]");
  const catalogueLists = {
    robot: root.querySelector("[data-catalogue-list='robot']"),
    task: root.querySelector("[data-catalogue-list='task']"),
  };

  const matrixPresets = {
    intervention_minutes_per_robot_hour: [0, 3, 5, 8, 12, 16],
    utilization: [0.4, 0.5, 0.6, 0.67, 0.75, 0.85],
    productivity_vs_human: [0.75, 1, 1.25, 1.5, 2],
    asp: [20000, 35000, 50000, 75000, 100000],
    loaded_labor_cost_per_hour: [18, 25, 30, 40, 55, 75],
  };

  const labels = {
    intervention_minutes_per_robot_hour: "Intervention min/hr",
    utilization: "Utilization",
    productivity_vs_human: "Productivity",
    asp: "ASP",
    loaded_labor_cost_per_hour: "Labor/hr",
  };

  let robots = mergeCatalogue(seedRobots, readCatalogue(storageKeys.robots));
  let tasks = mergeCatalogue(seedTasks, readCatalogue(storageKeys.tasks));
  let selectedRobotId = robots[0]?.id || "";
  let selectedTaskId = tasks[0]?.id || "";
  let requestSeq = 0;
  let debounceTimer = null;

  function slug(value) {
    return String(value || "")
      .trim()
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "");
  }

  function readCatalogue(key) {
    try {
      const parsed = JSON.parse(localStorage.getItem(key) || "[]");
      return Array.isArray(parsed) ? parsed.filter((row) => row && typeof row === "object") : [];
    } catch (_error) {
      return [];
    }
  }

  function writeCatalogue(key, rows) {
    try {
      localStorage.setItem(key, JSON.stringify(rows.filter((row) => !row.seed)));
    } catch (_error) {}
  }

  function mergeCatalogue(seedRows, localRows) {
    const byId = new Map();
    seedRows.forEach((row) => byId.set(row.id, Object.assign({ seed: true }, row)));
    localRows.forEach((row) => {
      const id = row.id || slug([row.oem, row.platform, row.domain, row.task].filter(Boolean).join(" "));
      if (id) byId.set(id, Object.assign({}, row, { id, seed: false }));
    });
    return Array.from(byId.values());
  }

  function parseField(field) {
    if (field.type === "checkbox") return field.checked;
    const value = String(field.value || "").trim();
    if (!value) return null;
    if (field.type === "number") {
      const parsed = Number(value);
      return Number.isFinite(parsed) ? parsed : null;
    }
    return value;
  }

  function setField(name, value) {
    const field = form.elements.namedItem(name);
    if (!field || value === undefined || value === null) return;
    if (field.type === "checkbox") {
      field.checked = Boolean(value);
      return;
    }
    field.value = value;
  }

  function populateRegionalPresets() {
    if (!regionPresetSelect) return;
    regionPresetSelect.innerHTML = [
      `<option value="">Task / manual region</option>`,
      ...regionalPresets.map((preset) => `<option value="${escapeHtml(preset.id)}">${escapeHtml(preset.label)}</option>`),
    ].join("");
  }

  function applyRegionalPreset(presetId) {
    const preset = regionalPresets.find((row) => row.id === presetId);
    if (!preset) return;
    setField("region_name", preset.region_name || "");
    setField("currency", preset.currency || "");
    setField("loaded_labor_cost_per_hour", preset.loaded_labor_cost_per_hour || "");
    setField("wage_per_hour", preset.wage_per_hour || "");
    setField("payroll_tax_rate", preset.payroll_tax_rate || "");
    setField("benefits_uplift_rate", preset.benefits_uplift_rate || "");
    setField("other_labor_uplift_rate", preset.other_labor_uplift_rate || "");
    scheduleRefresh();
  }

  function selectedRobot() {
    return robots.find((robot) => robot.id === selectedRobotId) || robots[0] || {};
  }

  function selectedTask() {
    return tasks.find((task) => task.id === selectedTaskId) || tasks[0] || {};
  }

  function applySelectionToForm() {
    const robot = selectedRobot();
    const task = selectedTask();
    setField("robot_oem", robot.oem || "");
    setField("robot_platform", robot.platform || "");
    setField("asp", robot.asp || "");
    setField("useful_life_years", robot.useful_life_years || 2);
    setField("domain", task.domain || "");
    setField("task", task.task || "");
    setField("region_name", task.region_name || "United States");
    setField("loaded_labor_cost_per_hour", task.loaded_labor_cost_per_hour || "");
    setField("wage_per_hour", task.wage_per_hour || "");
    setField("payroll_tax_rate", task.payroll_tax_rate || "");
    setField("benefits_uplift_rate", task.benefits_uplift_rate || "");
    setField("other_labor_uplift_rate", task.other_labor_uplift_rate || "");
    setField("utilization", task.utilization || 0.67);
    setField("productivity_vs_human", task.productivity_vs_human || 1);
    setField("task_demand_coverage", task.task_demand_coverage || 1);
    setField("task_share_of_role", task.task_share_of_role || "");
    setField("intervention_minutes_per_robot_hour", task.intervention_minutes_per_robot_hour || 0);
    updateSelectedSummary();
    scheduleRefresh();
  }

  function buildPayload() {
    const payload = {};
    Array.from(form.elements).forEach((field) => {
      if (!field.name) return;
      const value = parseField(field);
      if (value === null || value === "") return;
      payload[field.name] = value;
    });
    matrixControls.forEach((control) => {
      payload[control.name] = control.value;
    });
    const rowVariable = payload.row_variable || "intervention_minutes_per_robot_hour";
    const columnVariable = payload.column_variable || "utilization";
    payload.include_matrix = true;
    payload.row_values = matrixPresets[rowVariable] || matrixPresets.intervention_minutes_per_robot_hour;
    payload.column_values = matrixPresets[columnVariable] || matrixPresets.utilization;
    return payload;
  }

  function money(value, currency) {
    if (!Number.isFinite(Number(value))) return "-";
    return new Intl.NumberFormat("en-US", {
      style: "currency",
      currency: currency || "USD",
      maximumFractionDigits: Number(value) >= 100 ? 0 : 1,
    }).format(Number(value));
  }

  function number(value, digits) {
    if (!Number.isFinite(Number(value))) return "-";
    return new Intl.NumberFormat("en-US", { maximumFractionDigits: digits }).format(Number(value));
  }

  function pct(value) {
    if (!Number.isFinite(Number(value))) return "-";
    return `${number(Number(value) * 100, 1)}%`;
  }

  function fmtByKey(key, value, currency) {
    if (key.includes("asp") || key.includes("cost") || key.includes("price") || key.includes("labor")) return money(value, currency);
    if (key.includes("utilization")) return pct(value);
    if (key.includes("productivity")) return `${number(value, 2)}x`;
    if (key.includes("minutes")) return `${number(value, 1)}m`;
    if (key.includes("hours")) return number(value, 0);
    return number(value, 2);
  }

  function setText(map, key, value) {
    const node = map.get(key);
    if (node) node.textContent = value;
  }

  function textForRobot(robot) {
    return `${robot.oem || "Unknown OEM"} ${robot.platform || "Unknown platform"}`.trim();
  }

  function textForTask(task) {
    return `${task.domain || "Unknown domain"} / ${task.task || "Unknown task"}`;
  }

  function updateSelectedSummary() {
    const robot = selectedRobot();
    const task = selectedTask();
    const currency = String(form.elements.namedItem("currency")?.value || "USD");
    selectedTitle.textContent = `${robot.platform || "Robot"} / ${task.domain || "Task"}`;
    setText(selectedNodes, "robot", textForRobot(robot));
    setText(selectedNodes, "task", textForTask(task));
    setText(selectedNodes, "asp", money(robot.asp, currency));
    setText(selectedNodes, "labor", `${money(task.loaded_labor_cost_per_hour, currency)}/hr`);
    renderGaps(robot, task);
  }

  function renderGaps(robot, task) {
    const gaps = [];
    if (!robot.asp) gaps.push("ASP");
    if (!robot.useful_life_years) gaps.push("useful life");
    if (!task.loaded_labor_cost_per_hour) gaps.push("labor rate");
    if (!task.productivity_vs_human) gaps.push("productivity");
    if (task.intervention_minutes_per_robot_hour === undefined || task.intervention_minutes_per_robot_hour === null) gaps.push("intervention burden");
    gapList.innerHTML = "";
    if (!gaps.length) {
      const pill = document.createElement("span");
      pill.className = "robotics-gap-pill complete";
      pill.textContent = "ready to model";
      gapList.appendChild(pill);
      return;
    }
    gaps.forEach((gap) => {
      const pill = document.createElement("span");
      pill.className = "robotics-gap-pill";
      pill.textContent = `missing ${gap}`;
      gapList.appendChild(pill);
    });
  }

  function catalogueSearch(kind) {
    const input = root.querySelector(`[data-catalogue-search="${kind}"]`);
    return String(input?.value || "").trim().toLowerCase();
  }

  function renderCatalogue(kind) {
    const rows = kind === "robot" ? robots : tasks;
    const selectedId = kind === "robot" ? selectedRobotId : selectedTaskId;
    const query = catalogueSearch(kind);
    const list = catalogueLists[kind];
    if (!list) return;
    const filtered = rows
      .filter((row) => {
        const text = kind === "robot"
          ? `${row.oem || ""} ${row.platform || ""} ${row.type || ""} ${row.source || ""}`
          : `${row.domain || ""} ${row.task || ""} ${row.role || ""} ${row.region_name || ""}`;
        return !query || text.toLowerCase().includes(query);
      })
      .slice(0, 12);
    list.innerHTML = "";
    filtered.forEach((row) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = `robotics-catalogue-item${row.id === selectedId ? " active" : ""}`;
      button.dataset.catalogueKind = kind;
      button.dataset.catalogueId = row.id;
      const remove = row.seed ? "" : `<span class="robotics-catalogue-remove" data-catalogue-delete="${row.id}" data-catalogue-delete-kind="${kind}">Remove</span>`;
      if (kind === "robot") {
        button.innerHTML = `<strong>${escapeHtml(textForRobot(row))}</strong><span>${escapeHtml(row.type || "Robot")} · ${money(row.asp, "USD")} · ${number(row.useful_life_years, 1)} yr</span>${remove}`;
      } else {
        button.innerHTML = `<strong>${escapeHtml(textForTask(row))}</strong><span>${escapeHtml(row.role || "Human role")} · ${money(row.loaded_labor_cost_per_hour, "USD")}/hr · ${pct(row.utilization)}</span>${remove}`;
      }
      list.appendChild(button);
    });
    if (!filtered.length) {
      const empty = document.createElement("div");
      empty.className = "robotics-catalogue-empty";
      empty.textContent = "No matches";
      list.appendChild(empty);
    }
  }

  function escapeHtml(value) {
    return String(value || "").replace(/[&<>"']/g, (char) => {
      const map = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" };
      return map[char] || char;
    });
  }

  function classifyCell(cost, labor) {
    if (!Number.isFinite(Number(cost)) || !Number.isFinite(Number(labor))) return "near";
    const ratio = Number(cost) / Number(labor);
    if (ratio <= 0.95) return "under";
    if (ratio <= 1.05) return "near";
    return "above";
  }

  function renderOutputs(result) {
    const outputs = result.outputs || {};
    const currency = result.scenario?.currency || "USD";
    const deployable = outputs.deployable_at_labor_parity;
    const advantage = Number(outputs.cost_advantage_per_human_equivalent_hour);
    statusPanel.classList.remove("above", "near");
    if (deployable === false) statusPanel.classList.add("above");
    if (Number.isFinite(advantage) && Math.abs(advantage) <= Number(outputs.regional_loaded_labor_cost_per_hour || 0) * 0.05) {
      statusPanel.classList.add("near");
    }
    setText(outputNodes, "deployable_at_labor_parity", deployable === null ? "Incomplete" : deployable ? "Below labor" : "Above labor");
    setText(outputNodes, "cost_advantage_per_human_equivalent_hour", `${money(advantage, currency)} vs loaded labor`);
    setText(outputNodes, "effective_cost_per_human_equivalent_hour", money(outputs.effective_cost_per_human_equivalent_hour, currency));
    setText(outputNodes, "customer_payback_months", Number.isFinite(Number(outputs.customer_payback_months)) ? `${number(outputs.customer_payback_months, 1)} mo` : "-");
    setText(outputNodes, "annual_productive_robot_hours", number(outputs.demand_limited_robot_hours ?? outputs.annual_productive_robot_hours, 0));

    const solve = result.solve_for || {};
    setText(solveNodes, "max_regional_asp_at_labor_parity", money(solve.max_regional_asp_at_labor_parity, currency));
    setText(solveNodes, "required_productivity_vs_human_at_labor_parity", `${number(solve.required_productivity_vs_human_at_labor_parity, 2)}x`);
    setText(solveNodes, "required_utilization_at_labor_parity", pct(solve.required_utilization_at_labor_parity));
    setText(solveNodes, "max_intervention_minutes_per_robot_hour_at_labor_parity", `${number(solve.max_intervention_minutes_per_robot_hour_at_labor_parity, 1)}m/hr`);
  }

  function renderMatrix(result) {
    const matrix = result.matrix;
    if (!matrix || !matrixTable) return;
    const currency = result.scenario?.currency || "USD";
    const labor = result.outputs?.regional_loaded_labor_cost_per_hour;
    const head = [`<thead><tr><th>${labels[matrix.row_variable] || matrix.row_variable}</th>`]
      .concat(matrix.column_values.map((value) => `<th>${fmtByKey(matrix.column_variable, value, currency)}</th>`))
      .concat(["</tr></thead>"])
      .join("");
    const body = matrix.cells
      .map((row, index) => {
        const label = fmtByKey(matrix.row_variable, matrix.row_values[index], currency);
        const cells = row
          .map((cell) => {
            const cost = cell.effective_cost_per_human_equivalent_hour;
            const cls = classifyCell(cost, labor);
            const payback = Number.isFinite(Number(cell.customer_payback_months)) ? `${number(cell.customer_payback_months, 1)} mo` : "no payback";
            return `<td class="${cls}"><span class="matrix-value">${money(cost, currency)}</span><span class="matrix-payback">${payback}</span></td>`;
          })
          .join("");
        return `<tr><th>${label}</th>${cells}</tr>`;
      })
      .join("");
    matrixTable.innerHTML = `${head}<tbody>${body}</tbody>`;
  }

  function renderAudit(result) {
    const audit = result.audit || {};
    const missing = audit.missing_required || [];
    const defaults = audit.defaults_used || {};
    const estimated = audit.estimated_fields || [];
    const warnings = audit.warnings || [];
    const completeness = audit.completeness || "unknown";
    auditSummary.textContent = `${completeness} assumptions. ${missing.length} missing required, ${Object.keys(defaults).length} defaults, ${estimated.length} estimated fields.`;
    const existing = auditPanel.querySelector(".robotics-audit-list");
    if (existing) existing.remove();
    const list = document.createElement("div");
    list.className = "robotics-audit-list";
    const items = [
      `completeness ${completeness}`,
      ...missing.map((item) => `missing ${item}`),
      ...Object.keys(defaults).map((item) => `default ${item}`),
      ...estimated.map((item) => `estimated ${item}`),
      ...warnings.map((item) => `warning ${item}`),
    ];
    items.slice(0, 18).forEach((item) => {
      const pill = document.createElement("span");
      pill.className = "robotics-audit-pill";
      pill.textContent = item;
      list.appendChild(pill);
    });
    auditPanel.appendChild(list);
  }

  async function refresh() {
    const seq = ++requestSeq;
    const payload = buildPayload();
    updateSelectedSummary();
    const response = await fetch("/api/robotics/deployment-model", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await response.json();
    if (seq !== requestSeq) return;
    if (!data.ok) throw new Error(data.error || "Model failed");
    renderOutputs(data.result);
    renderMatrix(data.result);
    renderAudit(data.result);
  }

  function scheduleRefresh() {
    window.clearTimeout(debounceTimer);
    debounceTimer = window.setTimeout(() => {
      refresh().catch((error) => {
        auditSummary.textContent = error.message || "Model failed";
      });
    }, 120);
  }

  function openDialog(kind) {
    const dialog = root.querySelector(`[data-dialog="${kind}"]`);
    if (dialog?.showModal) dialog.showModal();
  }

  function closeDialogs() {
    root.querySelectorAll("dialog[open]").forEach((dialog) => dialog.close());
  }

  function addRobot(formNode) {
    const data = Object.fromEntries(new FormData(formNode).entries());
    const row = {
      id: slug(`${data.oem} ${data.platform}`),
      oem: data.oem,
      platform: data.platform,
      type: data.type || "Robot",
      asp: Number(data.asp) || null,
      useful_life_years: Number(data.useful_life_years) || 2,
      source: data.source || "Working entry",
      seed: false,
    };
    robots = mergeCatalogue(seedRobots, readCatalogue(storageKeys.robots).concat(row));
    writeCatalogue(storageKeys.robots, robots);
    selectedRobotId = row.id;
    formNode.reset();
    closeDialogs();
    renderCatalogue("robot");
    applySelectionToForm();
  }

  function addTask(formNode) {
    const data = Object.fromEntries(new FormData(formNode).entries());
    const row = {
      id: slug(`${data.domain} ${data.task} ${data.region_name}`),
      domain: data.domain,
      task: data.task,
      role: data.role || "",
      region_name: data.region_name || "United States",
      loaded_labor_cost_per_hour: Number(data.loaded_labor_cost_per_hour) || null,
      utilization: Number(data.utilization) || 0.67,
      productivity_vs_human: Number(data.productivity_vs_human) || 1,
      intervention_minutes_per_robot_hour: Number(data.intervention_minutes_per_robot_hour) || 0,
      seed: false,
    };
    tasks = mergeCatalogue(seedTasks, readCatalogue(storageKeys.tasks).concat(row));
    writeCatalogue(storageKeys.tasks, tasks);
    selectedTaskId = row.id;
    formNode.reset();
    closeDialogs();
    renderCatalogue("task");
    applySelectionToForm();
  }

  root.querySelector("[data-action='reset-model']")?.addEventListener("click", () => {
    selectedRobotId = seedRobots[0].id;
    selectedTaskId = seedTasks[0].id;
    root.querySelectorAll("[data-catalogue-search]").forEach((input) => {
      input.value = "";
    });
    matrixControls.forEach((control) => {
      if (control.name === "row_variable") control.value = "intervention_minutes_per_robot_hour";
      if (control.name === "column_variable") control.value = "utilization";
    });
    renderCatalogue("robot");
    renderCatalogue("task");
    applySelectionToForm();
  });

  root.querySelector("[data-action='show-add-robot']")?.addEventListener("click", () => openDialog("robot"));
  root.querySelector("[data-action='show-add-task']")?.addEventListener("click", () => openDialog("task"));
  root.querySelectorAll("[data-action='close-dialog']").forEach((button) => button.addEventListener("click", closeDialogs));
  root.querySelector("[data-action='save-robot']")?.addEventListener("click", () => {
    const formNode = root.querySelector("[data-catalogue-form='robot']");
    const oem = formNode?.querySelector("input[name='oem']");
    const platform = formNode?.querySelector("input[name='platform']");
    if (!String(oem?.value || "").trim()) {
      oem?.focus();
      return;
    }
    if (!String(platform?.value || "").trim()) {
      platform?.focus();
      return;
    }
    addRobot(formNode);
  });
  root.querySelector("[data-action='save-task']")?.addEventListener("click", () => {
    const formNode = root.querySelector("[data-catalogue-form='task']");
    const domain = formNode?.querySelector("input[name='domain']");
    const task = formNode?.querySelector("input[name='task']");
    if (!String(domain?.value || "").trim()) {
      domain?.focus();
      return;
    }
    if (!String(task?.value || "").trim()) {
      task?.focus();
      return;
    }
    addTask(formNode);
  });

  root.querySelector("[data-catalogue-form='robot']")?.addEventListener("submit", (event) => {
    event.preventDefault();
    addRobot(event.currentTarget);
  });
  root.querySelector("[data-catalogue-form='task']")?.addEventListener("submit", (event) => {
    event.preventDefault();
    addTask(event.currentTarget);
  });

  root.addEventListener("click", (event) => {
    const remove = event.target.closest("[data-catalogue-delete]");
    if (remove) {
      event.stopPropagation();
      const kind = remove.dataset.catalogueDeleteKind;
      const id = remove.dataset.catalogueDelete;
      if (kind === "robot") {
        robots = robots.filter((row) => row.id !== id);
        writeCatalogue(storageKeys.robots, robots);
        selectedRobotId = robots[0]?.id || "";
        renderCatalogue("robot");
      }
      if (kind === "task") {
        tasks = tasks.filter((row) => row.id !== id);
        writeCatalogue(storageKeys.tasks, tasks);
        selectedTaskId = tasks[0]?.id || "";
        renderCatalogue("task");
      }
      applySelectionToForm();
      return;
    }
    const item = event.target.closest("[data-catalogue-kind]");
    if (!item) return;
    if (item.dataset.catalogueKind === "robot") selectedRobotId = item.dataset.catalogueId;
    if (item.dataset.catalogueKind === "task") selectedTaskId = item.dataset.catalogueId;
    renderCatalogue("robot");
    renderCatalogue("task");
    applySelectionToForm();
  });

  root.querySelectorAll("[data-catalogue-search]").forEach((input) => {
    input.addEventListener("input", () => renderCatalogue(input.dataset.catalogueSearch));
  });

  form.addEventListener("input", scheduleRefresh);
  form.addEventListener("change", scheduleRefresh);
  regionPresetSelect?.addEventListener("change", () => applyRegionalPreset(regionPresetSelect.value));
  matrixControls.forEach((control) => control.addEventListener("change", scheduleRefresh));

  populateRegionalPresets();
  renderCatalogue("robot");
  renderCatalogue("task");
  applySelectionToForm();
})();
