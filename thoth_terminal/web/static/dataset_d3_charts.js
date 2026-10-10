(function () {
  const MAX_POINTS = 2600;
  const COLORS = [
    "#a7c957",
    "#49b86f",
    "#13a67a",
    "#2f9ecb",
    "#f2b84b",
    "#ef5b5b",
    "#b879d8",
    "#7cc7ff",
    "#d6e68a",
    "#7a9c45",
  ];
  const DISPLAY_LABELS = {
    median_intvty: "Interactivity (tok/s/user)",
    mean_intvty: "Mean Interactivity (tok/s/user)",
    tput_per_gpu: "Token Throughput per GPU (tok/s/gpu)",
    input_tput_per_gpu: "Input Throughput per GPU (tok/s/gpu)",
    output_tput_per_gpu: "Output Throughput per GPU (tok/s/gpu)",
    concurrency: "Parallel Requests",
    hardware: "GPU Config",
    framework: "Serving Engine",
    spec_method: "MTP",
    __inferencex_config: "GPU Config",
    precision: "Precision",
    model: "Model",
    isl: "Input Length",
    osl: "Output Length",
    blended_text_usd_per_mtok: "Blended Text Cost ($/M tok)",
    output_usd_per_mtok: "Output Cost ($/M tok)",
    input_usd_per_mtok: "Input Cost ($/M tok)",
    intelligence_index: "Artificial Analysis Intelligence Index",
    median_output_tokens_per_second: "Output Speed (tok/s)",
    blended_revenue_usd_per_mtok: "Blended Revenue ($/M tok)",
    gross_margin_before_capex_usd_per_day_per_gpu: "Gross Margin / GPU / Day",
    value: "Value",
    value_number: "Value",
    period_date: "Period",
    as_of_date: "As of Date",
    document_date: "Document Date",
    event_date: "Event Date",
    model: "Model",
    provider: "Provider",
    output_usd_per_mtok: "Output Price ($/M tok)",
    revenue_ntd_thousand: "Revenue (NTD thousand)",
    arrival_lag_days: "Arrival Lag (days)",
    __row_count: "Row Count",
  };

  function readSchema(board) {
    const node = document.getElementById(board.dataset.schemaId || "");
    if (!node) return {};
    try {
      return JSON.parse(node.textContent || "{}");
    } catch (_err) {
      return {};
    }
  }

  function toNumber(value) {
    if (value === null || value === undefined || value === "") return null;
    const num = Number(String(value).replace(/[$,%]/g, ""));
    return Number.isFinite(num) ? num : null;
  }

  function toDate(value) {
    if (value === null || value === undefined || value === "") return null;
    const ms = Date.parse(String(value));
    return Number.isFinite(ms) ? new Date(ms) : null;
  }

  function labelFor(columns, key) {
    const col = columns.find((item) => item.key === key);
    return DISPLAY_LABELS[key] || (col && (col.label || col.key)) || key || "n/a";
  }

  function optionLabelFor(col) {
    return DISPLAY_LABELS[col.key] || col.label || col.key || "None";
  }

  function hardwareLabel(value) {
    const text = String(value ?? "").trim();
    if (!text) return "Unknown GPU";
    return text
      .replace(/\bgb(\d+)/gi, "GB$1")
      .replace(/\bb(\d+)/gi, "B$1")
      .replace(/\bh(\d+)/gi, "H$1")
      .replace(/\bmi(\d+)/gi, "MI$1")
      .toUpperCase();
  }

  function engineLabel(value) {
    const raw = String(value ?? "").trim().toLowerCase();
    if (!raw || raw === "none") return "";
    if (raw === "vllm") return "vLLM";
    return raw
      .split(/[-_\s]+/)
      .filter(Boolean)
      .map((part) => {
        if (part === "trt") return "TRT";
        if (part === "vllm") return "vLLM";
        if (part === "sglang") return "SGLang";
        if (part === "mori") return "MoRI";
        if (part === "mtp") return "MTP";
        return part.charAt(0).toUpperCase() + part.slice(1);
      })
      .join(" ");
  }

  function specLabel(value) {
    const raw = String(value ?? "").trim().toLowerCase();
    if (!raw || raw === "none" || raw === "null") return "";
    if (raw === "mtp") return "MTP";
    return raw.toUpperCase();
  }

  function inferenceConfigLabel(values) {
    const gpu = hardwareLabel(values.hardware);
    const engine = engineLabel(values.framework);
    const spec = specLabel(values.spec_method);
    const suffix = [engine, spec].filter(Boolean).join(", ");
    return suffix ? `${gpu} (${suffix})` : gpu;
  }

  function displayValue(values, key) {
    if (key === "__inferencex_config") return inferenceConfigLabel(values || {});
    if (key === "hardware") return hardwareLabel((values || {}).hardware);
    if (key === "framework") return engineLabel((values || {}).framework) || "None";
    if (key === "spec_method") return specLabel((values || {}).spec_method) || "None";
    return String((values || {})[key] ?? "All");
  }

  function dataKeysFor(key) {
    if (key === "__inferencex_config") return ["hardware", "framework", "spec_method"];
    if (key === "__row_count") return [];
    return key ? [key] : [];
  }

  function numericColumns(columns) {
    return columns.filter((col) => {
      const type = String(col.data_type || "").toLowerCase();
      const role = String(col.role || "").toLowerCase();
      return ["number", "integer", "float", "decimal", "currency", "percent"].includes(type) || role === "metric";
    });
  }

  function dimensionColumns(columns) {
    return columns.filter((col) => !numericColumns(columns).some((num) => num.key === col.key));
  }

  function firstExisting(columns, keys) {
    const set = new Set(columns.map((col) => col.key));
    return keys.find((key) => set.has(key)) || "";
  }

  function inferDefaults(schema) {
    const columns = Array.isArray(schema.columns) ? schema.columns : [];
    const nums = numericColumns(columns);
    const dims = dimensionColumns(columns);
    const slug = String(schema.slug || "").toLowerCase();
    const defaults = {
      title: "Dataset Frontier",
      subtitle: "Interactive capped sample with column-level fetches.",
      x: nums[0]?.key || columns[0]?.key || "",
      y: nums[1]?.key || nums[0]?.key || columns[1]?.key || "",
      color: dims[0]?.key || "",
      size: nums[2]?.key || "",
      label: dims[1]?.key || dims[0]?.key || "",
      filterKeys: [],
      defaultFilter: "",
      xBetter: "max",
      yBetter: "max",
      chartType: "scatter",
      optimalOnly: false,
      logX: false,
      logY: false,
      labels: false,
      connect: false,
    };
    if (slug.includes("inferencex")) {
      defaults.title = "AgentX Throughput vs Interactivity";
      defaults.subtitle = "AgentX multi-turn traces only. Fixed-sequence results remain available in the dataset table.";
      defaults.x = firstExisting(columns, ["interactivity_tok_s_user", "median_intvty", "intvty", "interactivity"]) || defaults.x;
      defaults.y = firstExisting(columns, ["tput_per_gpu_tok_s", "tput_per_gpu", "throughput_tok_s_gpu", "throughput"]) || defaults.y;
      defaults.color = "__inferencex_config";
      defaults.size = firstExisting(columns, ["concurrency", "num_decode_gpu", "num_prefill_gpu"]) || "";
      defaults.label = "__inferencex_config";
      defaults.filterKeys = ["hardware", "framework", "spec_method", "model", "precision", "benchmark_type", "offload_mode", "recipe_fingerprint", "isl", "osl", "benchmark_ok", "error"].filter((key) => firstExisting(columns, [key]));
      defaults.defaultFilter = "inferencex";
      defaults.optimalOnly = true;
      defaults.connect = true;
    } else if (slug.includes("cost-quality-frontier") || slug.includes("artificial-analysis")) {
      defaults.title = "Intelligence vs Cost";
      defaults.subtitle = "Model quality and API economics from Artificial Analysis/provider pricing rows.";
      defaults.x = firstExisting(columns, ["blended_text_usd_per_mtok", "output_usd_per_mtok", "input_usd_per_mtok", "usd_per_1m_tokens"]) || defaults.x;
      defaults.y = firstExisting(columns, ["intelligence_index", "artificial_analysis_intelligence_index", "quality_score"]) || defaults.y;
      defaults.color = firstExisting(columns, ["creator", "provider", "model_creator"]) || defaults.color;
      defaults.size = firstExisting(columns, ["median_output_tokens_per_second", "output_tokens_per_second", "speed"]) || defaults.size;
      defaults.label = firstExisting(columns, ["model", "model_name"]) || defaults.label;
      defaults.logX = true;
      defaults.labels = true;
      defaults.xBetter = "min";
      defaults.optimalOnly = true;
    } else if (slug.includes("serving-margin")) {
      defaults.title = "Serving Margin Scenario Surface";
      defaults.subtitle = "Revenue, throughput, utilization, cache, and capex assumptions in one capped chart.";
      defaults.x = firstExisting(columns, ["blended_revenue_usd_per_mtok", "revenue_usd_per_mtok", "realized_price_usd_per_mtok"]) || defaults.x;
      defaults.y = firstExisting(columns, ["gross_margin_before_capex_usd_per_day_per_gpu", "gross_profit_usd_per_day_per_gpu", "operating_margin_usd_per_day_per_gpu"]) || defaults.y;
      defaults.color = firstExisting(columns, ["hardware", "gpu", "priced_provider", "scenario_id"]) || defaults.color;
      defaults.size = firstExisting(columns, ["tput_per_gpu_tok_s", "throughput_tok_s_gpu"]) || defaults.size;
      defaults.label = firstExisting(columns, ["priced_model", "benchmark_model", "model"]) || defaults.label;
      defaults.optimalOnly = true;
    } else if (slug.includes("provider-api-pricing") || schema.family === "ai_provider_pricing") {
      defaults.title = "API Price by Model";
      defaults.subtitle = "Provider pricing rows shown as comparable input/output token prices.";
      defaults.chartType = "bar";
      defaults.x = firstExisting(columns, ["model", "model_family"]) || defaults.x;
      defaults.y = firstExisting(columns, ["output_usd_per_mtok", "input_usd_per_mtok"]) || defaults.y;
      defaults.color = firstExisting(columns, ["provider", "service_tier"]) || defaults.color;
      defaults.label = firstExisting(columns, ["model", "provider"]) || defaults.label;
    } else if (schema.family === "ai_serving_economics") {
      defaults.title = "Serving Assumptions";
      defaults.subtitle = "Scenario assumptions rendered as a compact comparison bar chart.";
      defaults.chartType = "bar";
      defaults.x = firstExisting(columns, ["scenario_label", "scenario_id"]) || defaults.x;
      defaults.y = firstExisting(columns, ["gpu_capex_usd", "utilization_rate", "cache_hit_rate"]) || defaults.y;
      defaults.color = firstExisting(columns, ["assumption_status", "scenario_label"]) || defaults.color;
    } else if (schema.family === "trade_customs" || schema.family === "trade") {
      defaults.title = "Trade Time Series";
      defaults.subtitle = "Period values by flow, HS code, reporter, or partner where present.";
      defaults.chartType = "line";
      defaults.connect = true;
      defaults.x = firstExisting(columns, ["period_date", "date"]) || defaults.x;
      defaults.y = firstExisting(columns, ["value", "value_usd"]) || defaults.y;
      defaults.color = firstExisting(columns, ["flow", "hs_code", "partner_iso", "reporter_iso"]) || defaults.color;
    } else if (schema.family === "earnings_actuals" || schema.family === "guidance_actuals" || schema.family === "monthly_revenue") {
      defaults.title = schema.family === "monthly_revenue" ? "Monthly Revenue Trend" : "Earnings Metric Trend";
      defaults.subtitle = "Time-series metrics grouped by metric or issuer dimension.";
      defaults.chartType = "line";
      defaults.connect = true;
      defaults.x = firstExisting(columns, ["period_date", "as_of_date", "date"]) || defaults.x;
      defaults.y = firstExisting(columns, ["revenue_ntd_thousand", "value_number", "value"]) || defaults.y;
      defaults.color = firstExisting(columns, ["metric_key", "company_name", "company", "unit"]) || defaults.color;
    } else if (schema.family === "earnings_filings") {
      defaults.title = "Filing Arrival Timeline";
      defaults.subtitle = "Filing ledger rows counted by document date; select arrival lag when latency matters.";
      defaults.chartType = "bar";
      defaults.x = firstExisting(columns, ["document_date", "ingested_date"]) || defaults.x;
      defaults.y = "__row_count";
      defaults.color = firstExisting(columns, ["document_type", "connector"]) || defaults.color;
    } else if (schema.family === "ipo_calendar") {
      defaults.title = "IPO Calendar";
      defaults.subtitle = "Capital markets events summarized by event/listing date.";
      defaults.chartType = "bar";
      defaults.x = firstExisting(columns, ["event_date", "listing_date", "pricing_date"]) || defaults.x;
      defaults.y = "__row_count";
      defaults.color = firstExisting(columns, ["stage", "status", "market", "exchange"]) || defaults.color;
    } else if (schema.family === "future_electronics_product_availability") {
      defaults.title = "Product Availability";
      defaults.subtitle = "Stock and pricing rows summarized by manufacturer or product category.";
      defaults.chartType = "bar";
      defaults.x = firstExisting(columns, ["manufacturer", "category", "stock_status"]) || defaults.x;
      defaults.y = firstExisting(columns, ["global_stock", "best_unit_price"]) || defaults.y;
      defaults.color = firstExisting(columns, ["stock_status", "category", "manufacturer"]) || defaults.color;
    }
    return defaults;
  }

  function option(select, columns, value, allowNone) {
    const items = allowNone ? [{ key: "", label: "None" }].concat(columns) : columns;
    select.innerHTML = items
      .map((col) => `<option value="${escapeHtml(col.key)}"${col.key === value ? " selected" : ""}>${escapeHtml(optionLabelFor(col))}</option>`)
      .join("");
  }

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
  }

  function formatNumber(value) {
    if (value === null || value === undefined || !Number.isFinite(value)) return "n/a";
    const abs = Math.abs(value);
    if (abs >= 1e9) return `${(value / 1e9).toFixed(1)}b`;
    if (abs >= 1e6) return `${(value / 1e6).toFixed(1)}m`;
    if (abs >= 1e3) return `${(value / 1e3).toFixed(1)}k`;
    if (abs > 0 && abs < 0.01) return value.toExponential(1);
    return Number(value.toFixed(abs < 10 ? 2 : 1)).toLocaleString();
  }

  function selectedKeys(state) {
    return Array.from(new Set([state.x, state.y, state.color, state.size, state.label].concat(state.filterKeys || []).flatMap(dataKeysFor).filter(Boolean)));
  }

  async function fetchRows(board, state) {
    const params = new URLSearchParams();
    params.set("limit", String(state.limit || 1500));
    const viewId = String(board.dataset.viewId || "").trim();
    if (viewId) params.set("view_id", viewId);
    const keys = selectedKeys(state);
    if (keys.length) params.set("columns", keys.join(","));
    const resp = await fetch(`/api/datasets/${encodeURIComponent(board.dataset.datasetId)}/chart-data?${params.toString()}`);
    const data = await resp.json();
    if (!resp.ok || !data.ok) throw new Error(data.error || "Unable to load chart data");
    return data.dataset || {};
  }

  function autoInferenceFilter(rows, state) {
    if (state.defaultFilter !== "inferencex") return rows;
    const usable = rows.filter((row) => {
      const values = row.values || {};
      return values.benchmark_type === "agentic_traces" && values.benchmark_ok !== false && !String(values.error || "").trim();
    });
    if (!usable.length) return [];
    if (!state.autoFilter) {
      const counts = new Map();
      usable.forEach((row) => {
        const values = row.values || {};
        const tuple = [values.model, values.precision, values.isl, values.osl].map((value) => String(value ?? "")).join("\u0001");
        counts.set(tuple, (counts.get(tuple) || 0) + 1);
      });
      const ranked = Array.from(counts.entries()).sort((a, b) => {
        const ap = a[0].includes("\u00018192\u00011024") ? 1 : 0;
        const bp = b[0].includes("\u00018192\u00011024") ? 1 : 0;
        return bp - ap || b[1] - a[1];
      });
      const parts = String(ranked[0]?.[0] || "").split("\u0001");
      state.autoFilter = { model: parts[0], precision: parts[1], isl: parts[2], osl: parts[3] };
    }
    return usable.filter((row) => {
      const values = row.values || {};
      return ["model", "precision", "isl", "osl"].every((key) => String(values[key] ?? "") === String(state.autoFilter[key] ?? ""));
    });
  }

  function filteredRows(rows, state) {
    const scoped = autoInferenceFilter(rows, state);
    const q = String(state.search || "").trim().toLowerCase();
    const searched = q
      ? scoped.filter((row) => Object.values(row.values || {}).some((value) => String(value ?? "").toLowerCase().includes(q)))
      : scoped;
    const seriesFiltered = searched.filter((row) => {
      if (!state.selectedSeries || !state.selectedSeries.size || !state.color) return true;
      const key = displayValue(row.values || {}, state.color);
      return state.selectedSeries.has(key);
    });
    return state.optimalOnly ? optimalRows(seriesFiltered, state) : seriesFiltered;
  }

  function optimalRows(rows, state) {
    const points = rows.map((row, index) => {
      const values = row.values || {};
      return { row, index, x: toNumber(values[state.x]), y: toNumber(values[state.y]) };
    }).filter((point) => point.x !== null && point.y !== null);
    const keep = new Set();
    points.forEach((point) => {
      const dominated = points.some((other) => {
        if (other.index === point.index) return false;
        const xDominates = state.xBetter === "min" ? other.x <= point.x : other.x >= point.x;
        const xStrict = state.xBetter === "min" ? other.x < point.x : other.x > point.x;
        const yDominates = state.yBetter === "min" ? other.y <= point.y : other.y >= point.y;
        const yStrict = state.yBetter === "min" ? other.y < point.y : other.y > point.y;
        return xDominates && yDominates && (xStrict || yStrict);
      });
      if (!dominated) keep.add(point.index);
    });
    return rows.filter((_row, index) => keep.has(index));
  }

  function isDateColumn(columns, key) {
    const col = columns.find((item) => item.key === key);
    const type = String(col?.data_type || "").toLowerCase();
    const role = String(col?.role || "").toLowerCase();
    return role === "timestamp" || ["date", "datetime", "timestamp"].includes(type) || /(^|_)(date|at)$/.test(String(key || ""));
  }

  function sampledRows(rows) {
    if (rows.length <= MAX_POINTS) return rows;
    const stride = Math.ceil(rows.length / MAX_POINTS);
    return rows.filter((_row, index) => index % stride === 0).slice(0, MAX_POINTS);
  }

  function renderTable(host, rows, columns) {
    const shown = rows.slice(0, 220);
    host.innerHTML = `
      <table class="d3-data-table">
        <thead><tr>${columns.map((col) => `<th>${escapeHtml(col.label || col.key)}</th>`).join("")}</tr></thead>
        <tbody>${shown
          .map((row) => `<tr>${columns.map((col) => `<td>${escapeHtml((row.values || {})[col.key] ?? "")}</td>`).join("")}</tr>`)
          .join("")}</tbody>
      </table>
    `;
  }

  function renderBarChart(board, dataset, state) {
    const host = board.querySelector("[data-chart-host]");
    const status = board.querySelector("[data-status]");
    const columns = Array.isArray(dataset.columns) ? dataset.columns : [];
    const rawRows = filteredRows(Array.isArray(dataset.rows) ? dataset.rows : [], { ...state, optimalOnly: false });
    const grouped = new Map();
    rawRows.forEach((row) => {
      const values = row.values || {};
      const xRaw = values[state.x];
      const xKey = isDateColumn(columns, state.x) && toDate(xRaw) ? toDate(xRaw).toISOString().slice(0, 10) : String(xRaw ?? "n/a");
      const yVal = state.y === "__row_count" ? 1 : toNumber(values[state.y]);
      const current = grouped.get(xKey) || { key: xKey, value: 0, count: 0, color: displayValue(values, state.color), sample: values };
      current.value += yVal === null ? 1 : yVal;
      current.count += 1;
      grouped.set(xKey, current);
    });
    const sortedBars = Array.from(grouped.values()).sort((a, b) => {
      if (isDateColumn(columns, state.x)) return String(a.key).localeCompare(String(b.key));
      return b.value - a.value;
    });
    const bars = isDateColumn(columns, state.x) ? sortedBars.slice(-48) : sortedBars.slice(0, 48);
    host.innerHTML = "";
    const width = Math.max(720, host.clientWidth || 960);
    const height = Math.max(460, Math.min(680, Math.round(width * 0.52)));
    const margin = { top: 18, right: 72, bottom: 92, left: 82 };
    const svg = d3.select(host).append("svg").attr("viewBox", `0 0 ${width} ${height}`).attr("class", state.highContrast ? "high-contrast" : "");
    const plot = svg.append("g").attr("transform", `translate(${margin.left},${margin.top})`);
    const innerW = width - margin.left - margin.right;
    const innerH = height - margin.top - margin.bottom;
    if (!bars.length) {
      plot.append("text").attr("x", 24).attr("y", 36).attr("fill", "#b9c0c8").text("No rows match the selected bar controls.");
      if (status) status.textContent = "0 visible bars";
      return;
    }
    const x = d3.scaleBand().domain(bars.map((bar) => bar.key)).range([0, innerW]).padding(0.22);
    const y = d3.scaleLinear().domain([0, d3.max(bars, (bar) => bar.value) || 1]).range([innerH, 0]).nice();
    const color = d3.scaleOrdinal(COLORS).domain(Array.from(new Set(bars.map((bar) => bar.color))));
    plot.append("g").attr("class", "d3-grid").call(d3.axisLeft(y).ticks(7).tickSize(-innerW).tickFormat(""));
    plot.append("g").attr("class", "d3-axis").attr("transform", `translate(0,${innerH})`).call(d3.axisBottom(x).tickSizeOuter(0))
      .selectAll("text").attr("transform", "rotate(-35)").attr("text-anchor", "end").attr("dx", "-0.6em").attr("dy", "0.35em");
    plot.append("g").attr("class", "d3-axis").call(d3.axisLeft(y).ticks(7).tickFormat(formatNumber));
    const tooltip = d3.select(host).append("div").attr("class", "d3-tooltip");
    plot.selectAll("rect")
      .data(bars)
      .join("rect")
      .attr("x", (d) => x(d.key))
      .attr("y", (d) => y(d.value))
      .attr("width", x.bandwidth())
      .attr("height", (d) => innerH - y(d.value))
      .attr("rx", 3)
      .attr("fill", (d) => color(d.color))
      .attr("opacity", 0.88)
      .on("mousemove", function (event, d) {
        tooltip.style("display", "block")
          .style("left", `${event.offsetX + 14}px`)
          .style("top", `${event.offsetY + 12}px`)
          .html(`<b>${escapeHtml(d.key)}</b><br>${escapeHtml(labelFor(columns, state.y))}: ${formatNumber(d.value)}<br>Rows: ${d.count.toLocaleString()}${state.color ? `<br>${escapeHtml(labelFor(columns, state.color))}: ${escapeHtml(d.color)}` : ""}`);
      })
      .on("mouseleave", () => tooltip.style("display", "none"));
    svg.append("text").attr("class", "d3-axis-title").attr("x", margin.left + innerW / 2).attr("y", height - 14).attr("text-anchor", "middle").text(labelFor(columns, state.x));
    svg.append("text").attr("class", "d3-axis-title").attr("transform", "rotate(-90)").attr("x", -(margin.top + innerH / 2)).attr("y", 18).attr("text-anchor", "middle").text(labelFor(columns, state.y));
    if (status) status.textContent = `${bars.length.toLocaleString()} bars from ${rawRows.length.toLocaleString()} matching rows. Values are summed; nonnumeric rows count as 1.`;
  }

  function renderChart(board, dataset, state) {
    const host = board.querySelector("[data-chart-host]");
    const status = board.querySelector("[data-status]");
    const columns = Array.isArray(dataset.columns) ? dataset.columns : [];
    if (state.chartType === "bar") {
      renderBarChart(board, dataset, state);
      return;
    }
    const xIsDate = isDateColumn(columns, state.x);
    const rows = sampledRows(filteredRows(Array.isArray(dataset.rows) ? dataset.rows : [], state)).map((row) => {
      const values = row.values || {};
      return {
        row,
        x: xIsDate ? toDate(values[state.x]) : toNumber(values[state.x]),
        y: toNumber(values[state.y]),
        size: toNumber(values[state.size]),
        color: displayValue(values, state.color),
        label: displayValue(values, state.label),
      };
    }).filter((point) => {
      if (point.x === null || point.y === null) return false;
      if (!xIsDate && state.logX && point.x <= 0) return false;
      if (state.logY && point.y <= 0) return false;
      return true;
    });
    host.innerHTML = "";
    const width = Math.max(720, host.clientWidth || 960);
    const height = Math.max(460, Math.min(680, Math.round(width * 0.52)));
    const margin = { top: 18, right: 170, bottom: 58, left: 72 };
    const svg = d3.select(host).append("svg").attr("viewBox", `0 0 ${width} ${height}`).attr("class", state.highContrast ? "high-contrast" : "");
    const plot = svg.append("g").attr("transform", `translate(${margin.left},${margin.top})`);
    const innerW = width - margin.left - margin.right;
    const innerH = height - margin.top - margin.bottom;
    if (!rows.length) {
      plot.append("text").attr("x", 24).attr("y", 36).attr("fill", "#b9c0c8").text("No numeric rows match the selected chart controls.");
      if (status) status.textContent = "0 visible rows";
      return;
    }
    const xExtent = d3.extent(rows, (d) => d.x);
    const yExtent = d3.extent(rows, (d) => d.y);
    const x = xIsDate
      ? d3.scaleTime().domain(xExtent).range([0, innerW]).nice()
      : (state.logX ? d3.scaleLog() : d3.scaleLinear()).domain(expandDomain(xExtent, state.logX)).range([0, innerW]).nice();
    const y = (state.logY ? d3.scaleLog() : d3.scaleLinear()).domain(expandDomain(yExtent, state.logY)).range([innerH, 0]).nice();
    const color = d3.scaleOrdinal(COLORS);
    color.domain(Array.from(new Set(rows.map((row) => row.color))));
    const sizeExtent = d3.extent(rows, (d) => d.size || 1);
    const radius = d3.scaleSqrt().domain(expandDomain(sizeExtent, false)).range([3.2, 9]);
    const gx = plot.append("g").attr("class", "d3-axis").attr("transform", `translate(0,${innerH})`);
    const gy = plot.append("g").attr("class", "d3-axis");
    const gridX = plot.append("g").attr("class", "d3-grid").attr("transform", `translate(0,${innerH})`);
    const gridY = plot.append("g").attr("class", "d3-grid");
    const seriesLayer = plot.append("g").attr("class", "d3-series-layer");
    const pointLayer = plot.append("g").attr("class", "d3-point-layer");
    const labelLayer = plot.append("g").attr("class", "d3-label-layer");
    const tooltip = d3.select(host).append("div").attr("class", "d3-tooltip");

    function draw(zx, zy) {
      gx.call(d3.axisBottom(zx).ticks(8).tickFormat(xIsDate ? d3.timeFormat("%Y-%m-%d") : formatNumber));
      gy.call(d3.axisLeft(zy).ticks(8).tickFormat(formatNumber));
      gridX.call(d3.axisBottom(zx).ticks(8).tickSize(-innerH).tickFormat(""));
      gridY.call(d3.axisLeft(zy).ticks(8).tickSize(-innerW).tickFormat(""));
      seriesLayer.selectAll("*").remove();
      labelLayer.selectAll("*").remove();
      if ((state.connect || state.chartType === "line") && state.color) {
        const line = d3.line().defined((d) => d.x !== null && d.y !== null).x((d) => zx(d.x)).y((d) => zy(d.y));
        const groups = d3.group(rows, (d) => d.color);
        seriesLayer.selectAll("path")
          .data(Array.from(groups, ([key, values]) => ({ key, values: values.slice().sort((a, b) => Number(a.x) - Number(b.x)) })))
          .join("path")
          .attr("fill", "none")
          .attr("stroke", (d) => color(d.key))
          .attr("stroke-width", 1.8)
          .attr("opacity", 0.72)
          .attr("d", (d) => line(d.values));
      }
      const dots = pointLayer.selectAll("circle").data(rows, (d, i) => `${d.row.row_key || i}`);
      dots.join("circle")
        .attr("cx", (d) => zx(d.x))
        .attr("cy", (d) => zy(d.y))
        .attr("r", (d) => radius(d.size || 1))
        .attr("fill", (d) => color(d.color))
        .attr("stroke", state.highContrast ? "#f8fafc" : "rgba(12,14,18,.9)")
        .attr("stroke-width", state.highContrast ? 1.4 : 0.7)
        .attr("opacity", 0.88)
        .on("mousemove", function (event, d) {
          tooltip.style("display", "block")
            .style("left", `${event.offsetX + 14}px`)
            .style("top", `${event.offsetY + 12}px`)
            .html(`<b>${escapeHtml(d.label || d.color)}</b><br>${escapeHtml(labelFor(columns, state.x))}: ${xIsDate ? d3.timeFormat("%Y-%m-%d")(d.x) : formatNumber(d.x)}<br>${escapeHtml(labelFor(columns, state.y))}: ${formatNumber(d.y)}${state.size ? `<br>${escapeHtml(labelFor(columns, state.size))}: ${formatNumber(d.size)}` : ""}`);
        })
        .on("mouseleave", () => tooltip.style("display", "none"));
      if (state.labels) {
        labelLayer.selectAll("text")
          .data(rows.slice().sort((a, b) => Math.abs(b.y) - Math.abs(a.y)).slice(0, 36).filter((d) => d.label))
          .join("text")
          .attr("x", (d) => zx(d.x) + 7)
          .attr("y", (d) => zy(d.y) - 7)
          .attr("fill", "#dfe6ee")
          .attr("font-size", 10)
          .attr("paint-order", "stroke")
          .attr("stroke", "#0f1115")
          .attr("stroke-width", 3)
          .text((d) => d.label.slice(0, 26));
      }
    }

    svg.append("text").attr("class", "d3-axis-title").attr("x", margin.left + innerW / 2).attr("y", height - 14).attr("text-anchor", "middle").text(labelFor(columns, state.x));
    svg.append("text").attr("class", "d3-axis-title").attr("transform", "rotate(-90)").attr("x", -(margin.top + innerH / 2)).attr("y", 18).attr("text-anchor", "middle").text(labelFor(columns, state.y));
    draw(x, y);
    const legendItems = Array.from(new Set(rows.map((d) => d.color))).slice(0, 18);
    const legend = svg.append("g").attr("class", "d3-legend").attr("transform", `translate(${width - margin.right + 20},${margin.top + 8})`);
    legendItems.forEach((item, index) => {
      const g = legend.append("g").attr("transform", `translate(0,${index * 21})`);
      g.append("circle").attr("r", 5).attr("fill", color(item));
      g.append("text").attr("x", 11).attr("y", 4).text(item.slice(0, 28));
    });
    svg.call(d3.zoom().scaleExtent([0.8, 24]).translateExtent([[0, 0], [width, height]]).on("zoom", (event) => {
      const zx = event.transform.rescaleX(x);
      const zy = event.transform.rescaleY(y);
      draw(zx, zy);
    }));
    if (status) {
      const raw = Array.isArray(dataset.rows) ? dataset.rows.length : 0;
      const filter = state.autoFilter ? ` Filter ${Object.values(state.autoFilter).filter(Boolean).join(" / ")}.` : "";
      const optimal = state.optimalOnly ? " Optimal frontier only." : "";
      status.textContent = `${rows.length.toLocaleString()} visible points from ${raw.toLocaleString()} fetched rows. Cap ${dataset.row_limit || state.limit}.${filter}${optimal}`;
    }
  }

  function seriesItems(rows, state) {
    const counts = new Map();
    rows.forEach((row) => {
      const values = row.values || {};
      const key = displayValue(values, state.color);
      counts.set(key, (counts.get(key) || 0) + 1);
    });
    return Array.from(counts.entries())
      .map(([key, count]) => ({ key, count }))
      .sort((a, b) => b.count - a.count || a.key.localeCompare(b.key));
  }

  function renderSeriesFilter(board, dataset, state, onChange) {
    const list = board.querySelector("[data-series-list]");
    if (!list) return;
    const rows = autoInferenceFilter(Array.isArray(dataset.rows) ? dataset.rows : [], state);
    const items = seriesItems(rows, state);
    if (!state.selectedSeries || state.seriesColumn !== state.color) {
      state.seriesColumn = state.color;
      state.selectedSeries = new Set(items.map((item) => item.key));
    }
    const query = String(state.seriesSearch || "").trim().toLowerCase();
    const shown = items.filter((item) => !query || item.key.toLowerCase().includes(query));
    list.innerHTML = shown.length
      ? shown.map((item, index) => {
          const color = COLORS[index % COLORS.length];
          const checked = state.selectedSeries.has(item.key) ? " checked" : "";
          return `
            <label class="d3-series-item">
              <input type="checkbox" value="${escapeHtml(item.key)}"${checked}>
              <span class="d3-series-dot" style="background:${color}"></span>
              <span class="d3-series-name">${escapeHtml(item.key)}</span>
              <span class="d3-series-count">${item.count.toLocaleString()}</span>
            </label>
          `;
        }).join("")
      : '<div class="d3-series-empty">No matching series</div>';
    list.querySelectorAll('input[type="checkbox"]').forEach((input) => {
      input.addEventListener("change", () => {
        if (input.checked) state.selectedSeries.add(input.value);
        else state.selectedSeries.delete(input.value);
        onChange();
      });
    });
  }

  function expandDomain(extent, log) {
    let [lo, hi] = extent;
    if (!Number.isFinite(lo) || !Number.isFinite(hi)) return log ? [1, 10] : [0, 1];
    if (log) {
      lo = Math.max(lo, Number.MIN_VALUE);
      hi = Math.max(hi, lo * 1.01);
      return [lo, hi];
    }
    if (lo === hi) {
      const pad = Math.abs(lo || 1) * 0.1;
      return [lo - pad, hi + pad];
    }
    const pad = (hi - lo) * 0.06;
    return [lo - pad, hi + pad];
  }

  function csv(rows, columns) {
    const escape = (value) => `"${String(value ?? "").replace(/"/g, '""')}"`;
    return [columns.map((col) => escape(col.label || col.key)).join(",")]
      .concat(rows.map((row) => columns.map((col) => escape((row.values || {})[col.key])).join(",")))
      .join("\n");
  }

  function downloadCsv(schema, dataset, state) {
    const columns = selectedKeys(state).map((key) => ({ key, label: labelFor(dataset.columns || schema.columns || [], key) }));
    const rows = filteredRows(Array.isArray(dataset.rows) ? dataset.rows : [], state);
    const blob = new Blob([csv(rows, columns)], { type: "text/csv;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${schema.slug || "dataset"}-chart-sample.csv`;
    a.click();
    URL.revokeObjectURL(a.href);
  }

  function initBoard(board) {
    if (!window.d3) return;
    const schema = readSchema(board);
    const columns = Array.isArray(schema.columns) ? schema.columns : [];
    const defaults = inferDefaults(schema);
    const state = { ...defaults, limit: Number(board.dataset.defaultLimit || 1500), search: "", seriesSearch: "", mode: "chart", highContrast: false, optimalOnly: !!defaults.optimalOnly };
    const controls = {
      chartType: board.querySelector('[data-control="chartType"]'),
      x: board.querySelector('[data-control="x"]'),
      y: board.querySelector('[data-control="y"]'),
      color: board.querySelector('[data-control="color"]'),
      size: board.querySelector('[data-control="size"]'),
      label: board.querySelector('[data-control="label"]'),
      limit: board.querySelector('[data-control="limit"]'),
      logX: board.querySelector('[data-control="logX"]'),
      optimalOnly: board.querySelector('[data-control="optimalOnly"]'),
      logY: board.querySelector('[data-control="logY"]'),
      labels: board.querySelector('[data-control="labels"]'),
      connect: board.querySelector('[data-control="connect"]'),
      highContrast: board.querySelector('[data-control="highContrast"]'),
    };
    const nums = numericColumns(columns);
    const inferenceVirtualColumns = defaults.defaultFilter === "inferencex"
      ? [{ key: "__inferencex_config", label: "GPU Config (Serving Engine, MTP)" }]
      : [];
    const metricVirtualColumns = [{ key: "__row_count", label: "Row Count" }];
    const colorColumns = inferenceVirtualColumns.concat(columns);
    const labelColumns = inferenceVirtualColumns.concat(columns);
    const yColumns = metricVirtualColumns.concat(nums.length ? nums : columns);
    controls.chartType.value = state.chartType || "scatter";
    option(controls.x, columns, state.x, false);
    option(controls.y, yColumns, state.y, false);
    option(controls.color, colorColumns, state.color, true);
    option(controls.size, nums, state.size, true);
    option(controls.label, labelColumns, state.label, true);
    controls.limit.value = String(state.limit);
    controls.logX.checked = !!state.logX;
    controls.optimalOnly.checked = !!state.optimalOnly;
    controls.logY.checked = !!state.logY;
    controls.labels.checked = !!state.labels;
    controls.connect.checked = !!state.connect;
    board.querySelector(".d3-board-title").textContent = defaults.title;
    board.querySelector(".d3-board-subtitle").textContent = `${defaults.subtitle} ${schema.profile?.row_count ? `${Number(schema.profile.row_count).toLocaleString()} source rows.` : ""}`;
    let dataset = { columns, rows: [] };
    let abort = null;

    async function refresh() {
      if (abort) abort.abort();
      abort = new AbortController();
      board.classList.add("is-loading");
      try {
        dataset = await fetchRows(board, state);
        render();
      } catch (err) {
        const host = board.querySelector("[data-chart-host]");
        host.innerHTML = `<div class="d3-chart-loading">${escapeHtml(err.message || "Unable to load chart data")}</div>`;
      } finally {
        board.classList.remove("is-loading");
      }
    }

    function render() {
      const rows = filteredRows(Array.isArray(dataset.rows) ? dataset.rows : [], state);
      const activeColumns = selectedKeys(state).map((key) => (dataset.columns || columns).find((col) => col.key === key)).filter(Boolean);
      board.querySelector("[data-chart-host]").hidden = state.mode !== "chart";
      board.querySelector("[data-table-host]").hidden = state.mode !== "table";
      renderSeriesFilter(board, dataset, state, render);
      if (state.mode === "chart") {
        renderChart(board, dataset, state);
      } else {
        renderTable(board.querySelector("[data-table-host]"), rows, activeColumns);
      }
    }

    Object.entries(controls).forEach(([key, el]) => {
      if (!el) return;
      el.addEventListener("change", () => {
        state[key] = el.type === "checkbox" ? el.checked : key === "limit" ? Number(el.value || 1500) : el.value;
        if (key === "color") state.selectedSeries = null;
        if (["x", "y", "color", "size", "label", "limit", "chartType"].includes(key)) refresh();
        else render();
      });
    });
    board.querySelector(".d3-search")?.addEventListener("input", (event) => {
      state.search = event.target.value || "";
      window.clearTimeout(board._d3SearchTimer);
      board._d3SearchTimer = window.setTimeout(render, 80);
    });
    board.querySelector(".d3-series-search")?.addEventListener("input", (event) => {
      state.seriesSearch = event.target.value || "";
      renderSeriesFilter(board, dataset, state, render);
    });
    board.querySelector('[data-action="series-reset"]')?.addEventListener("click", () => {
      const rows = autoInferenceFilter(Array.isArray(dataset.rows) ? dataset.rows : [], state);
      state.selectedSeries = new Set(seriesItems(rows, state).map((item) => item.key));
      state.seriesSearch = "";
      const input = board.querySelector(".d3-series-search");
      if (input) input.value = "";
      render();
    });
    board.querySelectorAll("[data-mode]").forEach((button) => {
      button.addEventListener("click", () => {
        state.mode = button.dataset.mode || "chart";
        board.querySelectorAll("[data-mode]").forEach((btn) => btn.classList.toggle("is-active", btn === button));
        render();
      });
    });
    board.querySelector('[data-action="download"]')?.addEventListener("click", () => downloadCsv(schema, dataset, state));
    board.querySelector('[data-action="reset"]')?.addEventListener("click", () => {
      Object.keys(state).forEach((key) => delete state[key]);
      Object.assign(state, { ...defaults, limit: Number(board.dataset.defaultLimit || 1500), search: "", seriesSearch: "", mode: "chart", highContrast: false, optimalOnly: !!defaults.optimalOnly });
      controls.chartType.value = state.chartType || "scatter";
      option(controls.x, columns, state.x, false);
      option(controls.y, yColumns, state.y, false);
      option(controls.color, colorColumns, state.color, true);
      option(controls.size, nums, state.size, true);
      option(controls.label, labelColumns, state.label, true);
      controls.limit.value = String(state.limit);
      controls.logX.checked = !!state.logX;
      controls.optimalOnly.checked = !!state.optimalOnly;
      controls.logY.checked = !!state.logY;
      controls.labels.checked = !!state.labels;
      controls.connect.checked = !!state.connect;
      controls.highContrast.checked = false;
      board.querySelector(".d3-search").value = "";
      board.querySelector(".d3-series-search").value = "";
      board.querySelectorAll("[data-mode]").forEach((btn) => btn.classList.toggle("is-active", btn.dataset.mode === "chart"));
      refresh();
    });
    refresh();
  }

  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll(".d3-analysis-board").forEach(initBoard);
  });
})();
