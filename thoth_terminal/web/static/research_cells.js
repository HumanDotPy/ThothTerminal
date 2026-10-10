/* Reusable review cells: data stays separate from rendering and theme tokens. */
(function () {
  "use strict";
  const NS = "http://www.w3.org/2000/svg";
  const number = (v) => v === null || v === undefined || typeof v === "boolean" || String(v).trim() === "" ? null : (Number.isFinite(Number(v)) ? Number(v) : null);
  const fmt = (v, digits = 1) => number(v) === null ? "—" : Number(v).toLocaleString("en-US", { maximumFractionDigits: digits });
  const short = (v) => number(v) === null ? "—" : window.d3.format("~s")(Number(v)).replace("G", "B");
  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }
  function theme(host) {
    const s = getComputedStyle(host);
    return { text: s.getPropertyValue("--cell-text").trim(), muted: s.getPropertyValue("--cell-muted").trim(), grid: s.getPropertyValue("--cell-grid").trim(), positive: s.getPropertyValue("--cell-positive").trim(), negative: s.getPropertyValue("--cell-negative").trim(), series: Array.from({ length: 6 }, (_, i) => s.getPropertyValue(`--cell-series-${i + 1}`).trim()) };
  }
  function note(host, text) { host.append(el("div", "cell-note", text)); }
  function dataDetails(host, title, text) { const details = el("details", "cell-data-details"); details.append(el("summary", "", title), el("p", "", text)); host.append(details); }
  function svg(host, height, label) {
    const width = Math.max(180, Math.floor(host.getBoundingClientRect().width));
    return { width, height, root: d3.select(host).append("svg").attr("width", width).attr("height", height).attr("role", "img").attr("aria-label", label) };
  }
  function tooltip(host) {
    const box = el("div", "cell-tooltip"); box.hidden = true; host.append(box);
    return { show(event, text) { box.textContent = text; box.hidden = false; const r = host.getBoundingClientRect(); const maxLeft = Math.max(8, r.width - box.offsetWidth - 8); box.style.left = `${Math.min(maxLeft, Math.max(8, event.clientX - r.left + 12))}px`; box.style.top = `${Math.max(8, Math.min(r.height - box.offsetHeight - 8, event.clientY - r.top - box.offsetHeight - 12))}px`; }, hide() { box.hidden = true; } };
  }
  function legend(host, series, colors) {
    const list = el("div", "cell-legend");
    series.forEach((s, i) => { const item = el("span", "cell-legend-item"); const swatch = el("i", "cell-swatch"); swatch.style.background = colors[i % colors.length]; item.append(swatch, document.createTextNode(s.label)); list.append(item); });
    host.append(list);
  }
  function yDomain(values, log, zero) {
    const valid = values.map(number).filter((n) => n !== null && (!log || n > 0));
    if (!valid.length) return null;
    let [lo, hi] = d3.extent(valid);
    if (log) return lo === hi ? [lo / 1.5, hi * 1.5] : [lo / 1.12, hi * 1.12];
    const pad = lo === hi ? Math.max(Math.abs(lo) * 0.1, 1) : (hi - lo) * 0.08;
    return zero ? [Math.min(0, lo), Math.max(0, hi) || 1] : [lo - pad, hi + pad];
  }
  function axes(root, x, y, frame, config) {
    const { left, top, width, height } = frame;
    const ticks = Math.max(3, Math.min(6, Math.floor(height / 55)));
    function logTicks(scale) {
      const [lo, hi] = scale.domain(); const span = Math.log10(hi / lo); const values = [];
      for (let power = Math.floor(Math.log10(lo)); power <= Math.ceil(Math.log10(hi)); power++) { (span > 2 ? [1] : [1, 2, 5]).forEach((m) => { const v = m * 10 ** power; if (v >= lo && v <= hi) values.push(v); }); }
      return values.filter((_, i) => i % Math.max(1, Math.ceil(values.length / 6)) === 0);
    }
    const yAxis = d3.axisLeft(y).ticks(ticks); const yGrid = d3.axisLeft(y).ticks(ticks).tickSize(-width).tickFormat("");
    if (config.logY) { yAxis.tickValues(logTicks(y)); yGrid.tickValues(logTicks(y)); }
    root.append("g").attr("class", "cell-grid").attr("transform", `translate(${left},${top})`).call(yGrid);
    root.append("g").attr("class", "cell-axis").attr("transform", `translate(${left},${top})`).call(yAxis.tickFormat(config.formatY || short).tickSize(0).tickPadding(10));
    const xAxis = d3.axisBottom(x).ticks(Math.max(3, Math.min(5, Math.floor(width / 110)))); if (config.logX) xAxis.tickValues(logTicks(x));
    if (config.xTicks) xAxis.tickValues(config.xTicks);
    const bottom = root.append("g").attr("class", "cell-axis").attr("transform", `translate(${left},${top + height})`).call(xAxis.tickFormat(config.formatX || short).tickSize(config.timeBoundary ? 4 : 0).tickPadding(10));
    if (config.timeBoundary) {
      bottom.select(".domain").attr("class", "cell-time-domain");
      const domain = x.domain(); bottom.selectAll(".tick text").attr("text-anchor", (date) => +date === +domain[0] ? "start" : +date === +domain[1] ? "end" : "middle");
    }
    if (config.yLabel) root.append("text").attr("class", "cell-axis-label").attr("x", left).attr("y", 14).text(config.yLabel);
    if (config.xLabel) root.append("text").attr("class", "cell-axis-label").attr("x", left + width).attr("y", top + height + 48).attr("text-anchor", "end").text(config.xLabel);
  }
  function line(host, spec, options) {
    if (host.getBoundingClientRect().width < 280) { note(host, "Expand this cell to show the timeline and latest values."); return; }
    const colors = theme(host); const log = options.scale === "log"; const rebased = options.scale === "indexed";
    const series = spec.series.map((s) => {
      let points = s.points.map((p) => ({ x: new Date(`${String(p.date).slice(0, 10)}T00:00:00Z`), y: number(p.value) })).filter((p) => Number.isFinite(p.x.getTime())).sort((a, b) => a.x - b.x);
      const base = points.find((p) => p.y !== null && p.y !== 0)?.y;
      if (rebased) points = points.map((p) => ({ ...p, y: p.y === null || !base ? null : p.y / base * 100 }));
      return { ...s, points };
    });
    const all = series.flatMap((s) => s.points); const domain = yDomain(all.map((p) => p.y), log, options.zero);
    if (!domain || !all.length) { note(host, "No usable observations for this scale."); return; }
    legend(host, series, colors.series);
    const chart = svg(host, options.compact ? 265 : 335, spec.title); const frame = { left: 58, top: 28, width: chart.width - 152, height: chart.height - 74 };
    const dates = d3.extent(all, (p) => p.x);
    const x = d3.scaleUtc().domain(dates).range([0, frame.width]);
    const y = (log ? d3.scaleLog() : d3.scaleLinear()).domain(domain).range([frame.height, 0]);
    if (!log) y.nice();
    const currency = !rebased && spec.unit === "USD";
    const valueLabel = (v) => currency ? `$${d3.format(".3~s")(v).replace("G", "B")}` : fmt(v);
    // Keep the real first and final dates, even when automatic ticks omit them.
    // Reject interior ticks that would collide with either boundary label.
    const interior = x.ticks(Math.max(2, Math.floor(frame.width / 95))).filter((d) => x(d) > 65 && frame.width - x(d) > 65);
    const dateTicks = [...new Map([dates[0], ...interior, dates[1]].map((d) => [+d, d])).values()];
    axes(chart.root, x, y, frame, { logY: log, formatY: valueLabel, formatX: d3.utcFormat("%b %y"), xTicks: dateTicks, timeBoundary: true });
    const plot = chart.root.append("g").attr("transform", `translate(${frame.left},${frame.top})`);
    series.forEach((s, i) => { const color = colors.series[i % 6]; const draw = d3.line().defined((p) => p.y !== null && (!log || p.y > 0)).x((p) => x(p.x)).y((p) => y(p.y)); plot.append("path").datum(s.points).attr("fill", "none").attr("stroke", color).attr("stroke-width", 2.3).attr("d", draw); });
    plot.append("line").attr("class", "cell-period-boundary").attr("x1", frame.width).attr("x2", frame.width).attr("y1", 0).attr("y2", frame.height);
    chart.root.append("text").attr("class", "cell-latest-period").attr("x", frame.left + frame.width + 10).attr("y", 14).text(d3.utcFormat("%b %Y")(dates[1]));
    const endings = series.map((s, i) => ({ point: s.points.at(-1), color: colors.series[i % 6] })).filter(({ point: p }) => p && +p.x === +dates[1] && p.y !== null && (!log || p.y > 0)).map((r) => ({ ...r, labelY: y(r.point.y) })).sort((a, b) => a.labelY - b.labelY);
    // A fixed endpoint column lets users read values without hovering. Separate
    // nearby labels, then connect each displaced label to its actual point.
    endings.forEach((r, i) => { r.labelY = Math.max(8, r.labelY, i ? endings[i - 1].labelY + 18 : 0); });
    for (let i = endings.length - 1; i >= 0; i--) endings[i].labelY = Math.min(endings[i].labelY, i === endings.length - 1 ? frame.height - 4 : endings[i + 1].labelY - 18);
    endings.forEach(({ point: p, color, labelY }) => {
      plot.append("circle").attr("cx", x(p.x)).attr("cy", y(p.y)).attr("r", 3).attr("fill", color);
      plot.append("line").attr("x1", frame.width + 4).attr("y1", y(p.y)).attr("x2", frame.width + 10).attr("y2", labelY).attr("stroke", color).attr("opacity", .6);
      plot.append("text").attr("class", "cell-end-value").attr("x", frame.width + 14).attr("y", labelY + 4).attr("fill", color).text(valueLabel(p.y));
    });
    const tip = tooltip(host); const guide = plot.append("line").attr("class", "cell-guide").attr("y1", 0).attr("y2", frame.height).style("display", "none");
    plot.append("rect").attr("width", frame.width).attr("height", frame.height).attr("fill", "transparent").on("pointermove", (event) => {
      const [px] = d3.pointer(event); const date = x.invert(px); guide.attr("x1", px).attr("x2", px).style("display", null);
      const lines = series.map((s) => { const i = d3.bisector((p) => p.x).center(s.points, date); const p = s.points[i]; return `${s.label} · ${d3.utcFormat("%b %Y")(p.x)}: ${fmt(p.y)}${rebased ? "" : ` ${spec.unit}`}`; }); tip.show(event, lines.join("\n"));
    }).on("pointerleave", () => { tip.hide(); guide.style("display", "none"); });
    const excluded = log ? all.filter((p) => p.y !== null && p.y <= 0).length : 0;
    dataDetails(host, "Data coverage", `${spec.asof || ""}. ${excluded ? `${excluded} nonpositive observations excluded from log. ` : ""}${rebased ? "Each series is rebased independently; this shows relative change." : "Straight segments between observations; missing observations remain gaps."}`);
  }
  function bar(host, spec, options) {
    const colors = theme(host); const rows = spec.rows.map((r) => ({ ...r, value: number(r.value) })).filter((r) => r.value !== null).sort((a, b) => b.value - a.value);
    if (!rows.length) { note(host, "No numeric values available."); return; }
    const height = Math.max(235, rows.length * (options.compact ? 29 : 36) + 65); const chart = svg(host, height, spec.title);
    const left = Math.min(135, Math.max(105, chart.width * 0.23)); const right = 76; const width = chart.width - left - right;
    const x = d3.scaleLinear().domain([Math.min(0, d3.min(rows, (r) => r.value)), Math.max(0, d3.max(rows, (r) => r.value)) || 1]).range([0, width]).nice();
    const y = d3.scaleBand().domain(rows.map((r) => r.label)).range([26, height - 42]).padding(0.35);
    const plot = chart.root.append("g").attr("transform", `translate(${left},0)`);
    plot.append("g").attr("class", "cell-grid").attr("transform", `translate(0,${height - 42})`).call(d3.axisBottom(x).ticks(Math.max(3, Math.floor(width / 90))).tickSize(-(height - 68)).tickFormat(""));
    plot.append("g").attr("class", "cell-axis").attr("transform", `translate(0,${height - 42})`).call(d3.axisBottom(x).ticks(Math.max(3, Math.floor(width / 90))).tickFormat(short).tickSize(0).tickPadding(10));
    plot.append("line").attr("class", "cell-guide").attr("x1", x(0)).attr("x2", x(0)).attr("y1", 22).attr("y2", height - 42);
    const tip = tooltip(host);
    rows.forEach((r) => {
      const middle = y(r.label) + y.bandwidth() / 2;
      chart.root.append("text").attr("class", "cell-bar-label").attr("x", left - 12).attr("y", middle + 4).attr("text-anchor", "end").text(r.label);
      const color = spec.signed ? (r.value < 0 ? colors.negative : colors.positive) : colors.series[0];
      plot.append("rect").attr("x", Math.min(x(0), x(r.value))).attr("y", y(r.label)).attr("width", Math.max(r.value === 0 ? 1 : 0, Math.abs(x(r.value) - x(0)))).attr("height", y.bandwidth()).attr("rx", 2).attr("fill", color);
      plot.append("text").attr("class", "cell-value").attr("x", spec.signed ? width + 7 : x(r.value) + 7).attr("y", middle + 4).attr("text-anchor", "start").text(`${spec.signed && r.value > 0 ? "+" : ""}${fmt(r.value)}${spec.signed ? "%" : ""}`);
      plot.append("rect").attr("x", 0).attr("y", y(r.label) - 5).attr("width", width).attr("height", y.bandwidth() + 10).attr("fill", "transparent").on("pointermove", (event) => tip.show(event, `${r.label}\n${fmt(r.value, 2)} ${spec.unit}`)).on("pointerleave", tip.hide);
    });
    chart.root.append("text").attr("class", "cell-axis-label").attr("x", left).attr("y", 14).text(spec.unit);
    if (rows.length !== spec.rows.length) note(host, `${spec.rows.length - rows.length} missing values omitted.`);
  }
  // Identity colors are independent of chart order and the user's accent color.
  // Light surfaces change luminance, preserving the provider's hue and role.
  const PROVIDERS = {
    OpenAI: ["OpenAI", "#c8d2cf", "#394943"],
    Anthropic: ["Anthropic", "#e6aa87", "#a35735"],
    Google: ["Google", "#7ec58b", "#307c43"],
    DeepSeek: ["DeepSeek", "#7ba9ff", "#3368c5"],
    SpaceXAI: ["xAI", "#c49dec", "#8559b3"],
    Alibaba: ["Alibaba", "#f1b95f", "#986310"],
    Kimi: ["Kimi", "#ee91b9", "#ab476e"],
    "Z AI": ["Z AI", "#f08e8b", "#b44449"],
    Meta: ["Meta", "#5dc9de", "#147f96"],
    Mistral: ["Mistral", "#e7cf68", "#887008"],
    Cohere: ["Cohere", "#86c5b3", "#387c67"],
    StepFun: ["StepFun", "#e8cd72", "#8e7418"],
    Apodex: ["Apodex", "#a0bddc", "#4b6d96"],
    MiniMax: ["MiniMax", "#dba5d4", "#96518c"]
  };
  function providerIdentity(creator, light) {
    const known = PROVIDERS[creator];
    if (known) return { label: known[0], color: known[light ? 2 : 1] };
    // Unknown creators retain a deterministic identity when the data order changes.
    let hash = 2166136261; for (const char of creator) hash = Math.imul(hash ^ char.charCodeAt(0), 16777619);
    return { label: creator, color: d3.hsl((hash >>> 0) % 360, .42, light ? .38 : .7).formatHex() };
  }
  function scatter(host, spec, options) {
    if (host.getBoundingClientRect().width < 280) { note(host, "Expand this cell to compare companies and read model labels."); return; }
    const log = options.scale === "log";
    const light = getComputedStyle(host).colorScheme === "light";
    const usable = spec.rows.map((r) => ({ ...r, creator: r.creator || "Unknown provider", x: number(r.x), y: number(r.y) })).filter((r) => r.x !== null && r.y !== null);
    if (!usable.length) { note(host, "No usable observations for this scale."); return; }
    const creators = [...new Set(usable.map((r) => r.creator))];
    const identity = (r) => providerIdentity(r.creator, light);
    const focus = creators.includes(options.providerFocus) ? options.providerFocus : null;
    const rank = (a, b) => b.y - a.y || a.x - b.x || a.label.localeCompare(b.label);
    const full = options.modelDetail === "all";
    const compared = focus ? [focus] : ["OpenAI", "Anthropic", "Google"].filter((creator) => creators.includes(creator));
    function currentFamilies(creator) {
      // Establish the newest generation from all records, including incomplete
      // ones. A missing new benchmark must not resurrect an old generation.
      const company = spec.rows.filter((r) => r.creator === creator);
      return [...d3.group(company, (r) => r.family || r.label.replace(/\s*\([^)]*\)/g, "")).values()].map((group) => {
        const dated = group.filter((r) => /^\d{4}-\d{2}-\d{2}$/.test(r.release || ""));
        if (!dated.length) return null;
        const newest = d3.max(dated, (r) => r.release);
        const evaluated = dated.filter((r) => r.release === newest).map((r) => ({ ...r, x: number(r.x), y: number(r.y) })).filter((r) => r.x !== null && r.y !== null).sort(rank);
        return evaluated[0] || null;
      }).filter(Boolean).sort((a, b) => b.release.localeCompare(a.release) || rank(a, b)).slice(0, 3);
    }
    const selected = full ? (focus ? usable.filter((r) => r.creator === focus) : usable) : compared.flatMap(currentFamilies);
    const rows = selected.filter((r) => !log || r.x > 0);
    const changeFocus = (creator) => options.onProviderFocus?.(creator === focus ? null : creator);
    const mode = el("div", "cell-model-mode");
    const caption = focus ? `${providerIdentity(focus, light).label} · ${full ? "all recorded models" : "up to 3 current families"}` : full ? "All recorded models" : "3 current families / company";
    mode.append(el("span", "", caption));
    const detail = el("select", ""); detail.setAttribute("aria-label", "Model comparison detail");
    [["families", "Current families"], ["all", "All models"]].forEach(([value, label]) => { const option = el("option", "", label); option.value = value; detail.append(option); });
    detail.value = full ? "all" : "families"; detail.addEventListener("change", () => options.onModelDetail?.(detail.value)); mode.append(detail); host.append(mode);
    const controls = el("div", "cell-provider-controls"); controls.setAttribute("aria-label", "Focus a model provider");
    const primary = Object.keys(PROVIDERS).slice(0, 8).filter((creator) => creators.includes(creator));
    primary.forEach((creator) => {
      const id = providerIdentity(creator, light); const button = el("button", "cell-provider-button"); button.type = "button";
      button.setAttribute("aria-pressed", String(focus === creator || !focus && !full && compared.includes(creator))); button.style.setProperty("--provider-color", id.color);
      const swatch = el("i", "cell-swatch"); swatch.style.background = id.color;
      button.append(swatch, document.createTextNode(id.label)); button.addEventListener("click", () => changeFocus(creator)); controls.append(button);
    });
    const remaining = creators.filter((creator) => !primary.includes(creator)).sort();
    if (remaining.length) {
      const select = el("select", "cell-provider-more"); select.setAttribute("aria-label", "Other model providers");
      const placeholder = el("option", "", `More providers (${remaining.length})`); placeholder.value = ""; select.append(placeholder);
      remaining.forEach((creator) => { const option = el("option", "", providerIdentity(creator, light).label); option.value = creator; select.append(option); });
      select.value = remaining.includes(focus) ? focus : ""; select.addEventListener("change", () => options.onProviderFocus?.(select.value || null)); controls.append(select);
    }
    if (focus) { const reset = el("button", "cell-provider-reset", "All providers"); reset.type = "button"; reset.addEventListener("click", () => options.onProviderFocus?.(null)); controls.append(reset); }
    host.append(controls);
    if (!rows.length) { note(host, selected.length ? "The selected observations have no positive price for a log axis. Switch to linear price to see them." : "No evaluated current families with known release dates are available for this company. Choose All models to inspect the recorded data."); return; }
    const chart = svg(host, options.compact ? 310 : 390, spec.title); const frame = { left: 45, top: 32, width: chart.width - 61, height: chart.height - 91 };
    const x = (log ? d3.scaleLog() : d3.scaleLinear()).domain(yDomain(rows.map((r) => r.x), log, !log)).range([0, frame.width]); if (!log) x.nice();
    const qualityDomain = yDomain(rows.map((r) => r.y), false, false);
    qualityDomain[0] -= (qualityDomain[1] - qualityDomain[0]) * .08;
    if (d3.min(rows, (r) => r.y) >= 0) qualityDomain[0] = Math.max(0, qualityDomain[0]);
    const y = d3.scaleLinear().domain(qualityDomain).range([frame.height, 0]).nice();
    axes(chart.root, x, y, frame, { xLabel: `${spec.xLabel}${log ? " · log" : ""}`, yLabel: spec.yLabel, logX: log, formatX: (v) => `$${fmt(v, 2)}` });
    const plot = chart.root.append("g").attr("transform", `translate(${frame.left},${frame.top})`);
    const active = focus ? rows.filter((r) => r.creator === focus) : rows;
    plot.selectAll("circle").data(rows).join("circle").attr("class", "cell-model-point").attr("data-provider", (r) => r.creator)
      .attr("cx", (r) => x(r.x)).attr("cy", (r) => y(r.y)).attr("r", (r) => focus && r.creator === focus ? 5 : 4)
      .attr("fill", (r) => identity(r).color).attr("opacity", (r) => focus && r.creator !== focus ? .12 : .78)
      .append("title").text((r) => `${identity(r).label} · ${r.label}`);

    // Reduced sets label every family where it fits; full-data mode prioritizes
    // nondominated and provider-best observations. Measure placements to avoid
    // covering text or points. Full variant names remain available on hover.
    const frontier = active.filter((r) => !active.some((other) => other.x <= r.x && other.y >= r.y && (other.x < r.x || other.y > r.y)));
    const best = [...d3.group(active, (r) => r.creator).values()].map((group) => d3.greatest(group, (r) => r.y));
    const candidates = full ? [...new Set([...frontier.sort((a, b) => b.y - a.y), ...best.sort((a, b) => b.y - a.y)])] : [...active].sort(rank);
    const occupied = []; const labelLimit = full ? Math.max(3, Math.min(9, Math.floor(frame.width / 60))) : rows.length;
    for (const r of candidates) {
      if (occupied.length >= labelLimit) break;
      const baseName = r.label.replace(/\s*\([^)]*\)/g, "");
      const effort = r.label.slice(baseName.length).match(/\b(max|xhigh|high|medium|low|minimal)\b/i);
      const displayName = `${baseName}${focus && effort ? ` · ${effort[1].toLowerCase()}` : ""}`;
      const label = plot.append("text").attr("class", "cell-model-label").attr("fill", identity(r).color).text(displayName);
      const maxWidth = Math.min(190, frame.width * .65);
      while (label.node().getComputedTextLength() > maxWidth && label.text().length > 6) label.text(label.text().replace(/…$/, "").slice(0, -1) + "…");
      const w = label.node().getComputedTextLength(); const px = x(r.x); const py = y(r.y);
      const locations = [[px + 9, py - 6], [px - w - 9, py - 6], [px + 9, py + 19], [px - w - 9, py + 19], [px - w / 2, py - 13], [px - w - 9, py - 28], [px - w - 9, py + 38], [px + 9, py - 28], [px + 9, py + 38], [px - w - 9, py - 46], [px + 9, py - 46], [px - w - 9, py + 55], [px + 9, py + 55]];
      const position = locations.find(([lx, ly]) => {
        const box = { x: lx - 3, y: ly - 12, w: w + 6, h: 17 };
        if (box.x < 0 || box.x + box.w > frame.width || box.y < 0 || box.y + box.h > frame.height) return false;
        if (occupied.some((other) => box.x < other.x + other.w && box.x + box.w > other.x && box.y < other.y + other.h && box.y + box.h > other.y)) return false;
        if (active.some((other) => other !== r && x(other.x) > box.x - 4 && x(other.x) < box.x + box.w + 4 && y(other.y) > box.y - 4 && y(other.y) < box.y + box.h + 4)) return false;
        return true;
      });
      if (!position) { label.remove(); continue; }
      if (Math.abs(position[1] - py) > 20) plot.append("line").attr("class", "cell-model-leader").attr("stroke", identity(r).color).attr("x1", px).attr("y1", py).attr("x2", Math.max(position[0], Math.min(position[0] + w, px))).attr("y2", position[1] - 5);
      label.attr("x", position[0]).attr("y", position[1]); label.append("title").text(`${identity(r).label} · ${r.label}`);
      occupied.push({ x: position[0] - 3, y: position[1] - 12, w: w + 6, h: 17 });
    }
    const tip = tooltip(host); const highlight = plot.append("circle").attr("class", "cell-model-highlight").attr("r", 7).style("display", "none");
    plot.append("rect").attr("width", frame.width).attr("height", frame.height).attr("fill", "transparent").on("pointermove", (event) => {
      const [px, py] = d3.pointer(event); const nearest = d3.least(active, (r) => (x(r.x) - px) ** 2 + (y(r.y) - py) ** 2);
      if (!nearest || Math.hypot(x(nearest.x) - px, y(nearest.y) - py) > 24) { tip.hide(); highlight.style("display", "none"); return; }
      highlight.attr("cx", x(nearest.x)).attr("cy", y(nearest.y)).attr("stroke", identity(nearest).color).style("display", null);
      tip.show(event, `${identity(nearest).label} · ${nearest.label}\n$${fmt(nearest.x, 2)} / million output tokens\n${fmt(nearest.y)} intelligence index\nReleased ${nearest.release || "unknown"}\nObserved ${String(nearest.observed || "unknown").slice(0, 10)}`);
    }).on("pointerleave", () => { tip.hide(); highlight.style("display", "none"); });
    const missing = spec.rows.length - usable.length;
    dataDetails(host, "Selection and data coverage", `${spec.asof || ""}. ${rows.length} plotted observations from ${usable.length} with a recorded price and intelligence index. Current families groups model lines, takes the newest released generation of each, then its highest-scoring evaluated variant; lower price breaks score ties. Up to three families per company are chosen by release date, then score. Undated families are excluded; incomplete new generations do not fall back to old ones. The default compares OpenAI, Anthropic and Google; company selection focuses one provider. Selection precedes log validation. ${missing} observations lack price or index; ${selected.length - rows.length} selected nonpositive prices are excluded by this scale. Full model variants and release dates are available on hover. This does not measure cost per task.`);
  }
  function share(host, spec, options) {
    const colors = theme(host); const rows = spec.rows.map((r, i) => ({ ...r, value: number(r.value), color: colors.series[i % 6] }));
    if (rows.some((r) => r.value === null || r.value < 0) || d3.sum(rows, (r) => r.value) <= 0) { note(host, "A part-to-whole view needs a defined positive total and valid nonnegative parts."); return; }
    const total = d3.sum(rows, (r) => r.value); const wrap = el("div", "cell-share-layout"); host.append(wrap);
    const chartHost = el("div", "cell-share-plot"); wrap.append(chartHost); const list = el("div", "cell-share-list"); wrap.append(list);
    const chart = svg(chartHost, options.compact ? 210 : 255, spec.title); const radius = Math.min(chart.width, chart.height) / 2 - 12;
    const arc = d3.arc().innerRadius(options.pie ? 0 : radius * 0.68).outerRadius(radius); const g = chart.root.append("g").attr("transform", `translate(${chart.width / 2},${chart.height / 2})`);
    const tip = tooltip(host);
    g.selectAll("path").data(d3.pie().sort(null).value((r) => r.value)(rows)).join("path").attr("d", arc).attr("fill", (r) => r.data.color).on("pointermove", (event, r) => tip.show(event, `${r.data.label}\n${fmt(r.data.value)} ${spec.unit}\n${fmt(r.data.value / total * 100)}% of selected total`)).on("pointerleave", tip.hide);
    if (!options.pie) { g.append("text").attr("class", "cell-donut-value").attr("text-anchor", "middle").attr("y", 0).text(fmt(total)); g.append("text").attr("class", "cell-axis-label").attr("text-anchor", "middle").attr("y", 21).text(spec.unit); }
    rows.forEach((r) => { const item = el("div", "cell-share-item"); const label = el("span", "cell-legend-item"); const swatch = el("i", "cell-swatch"); swatch.style.background = r.color; label.append(swatch, document.createTextNode(r.label)); item.append(label, el("strong", "cell-share-percent", `${fmt(r.value / total * 100)}%`), el("span", "cell-muted", `${fmt(r.value)} ${spec.unit}`)); list.append(item); });
    note(host, spec.denominator);
  }
  function table(host, spec) {
    const wrap = el("div", "cell-table-wrap"); const table = el("table", "cell-table"); const head = el("thead"); const tr = el("tr");
    spec.columns.forEach((c) => { const th = el("th", c.numeric ? "numeric" : "", c.label); if (c.unit) th.append(el("span", "cell-table-unit", c.unit)); th.scope = "col"; tr.append(th); }); head.append(tr); table.append(head); const body = el("tbody");
    spec.rows.forEach((r) => { const row = el("tr"); spec.columns.forEach((c) => { const v = r[c.key]; let text = c.numeric ? fmt(v, c.digits ?? 1) : String(v ?? "—"); if (c.signed && number(v) !== null) text = `${v > 0 ? "+" : ""}${fmt(v)}%`; const td = el("td", c.numeric ? "numeric" : "", text); if (c.signed && number(v) !== null && Number(v) !== 0) td.classList.add(v < 0 ? "negative" : "positive"); row.append(td); }); body.append(row); }); table.append(body); wrap.append(table); host.append(wrap);
  }
  function graph(host, spec) {
    const wrap = el("div", "cell-graph"); const edges = document.createElementNS(NS, "svg"); edges.classList.add("cell-graph-edges"); wrap.append(edges); const nodeEls = new Map();
    // Rank an acyclic graph by its actual links, so branches merge in a later
    // column even when both endpoints share a source layer label. Cycles fall
    // back to source layers rather than causing unbounded layout work.
    const ranks = new Map(spec.nodes.map((n) => [n.id, 0])); let changed = false;
    for (let pass = 0; pass < spec.nodes.length; pass++) { changed = false; spec.edges.forEach((e) => { if (ranks.has(e.source) && ranks.has(e.target) && ranks.get(e.target) <= ranks.get(e.source)) { ranks.set(e.target, ranks.get(e.source) + 1); changed = true; } }); if (!changed) break; }
    if (changed) { const sourceLayers = [...new Set(spec.nodes.map((n) => n.layer))]; spec.nodes.forEach((n) => ranks.set(n.id, sourceLayers.indexOf(n.layer))); }
    const layers = [...new Set(ranks.values())].sort((a, b) => a - b); const horizontal = host.getBoundingClientRect().width >= layers.length * 175;
    wrap.dataset.direction = horizontal ? "horizontal" : "vertical"; wrap.style.gridTemplateColumns = horizontal ? `repeat(${layers.length}, minmax(0, 1fr))` : "minmax(0, 1fr)";
    layers.forEach((rank) => { const group = spec.nodes.filter((n) => ranks.get(n.id) === rank); const lane = el("div", "cell-graph-lane"); lane.append(el("div", "cell-graph-layer", [...new Set(group.map((n) => n.layer.replaceAll("_", " ")))].join(" / "))); group.forEach((n) => { const item = el("div", "cell-graph-node"); item.append(el("strong", "", n.label)); if (n.detail) item.append(el("span", "cell-muted", n.detail)); item.title = `${n.label}\nStored ${spec.status} graph`; nodeEls.set(n.id, item); lane.append(item); }); wrap.append(lane); }); host.append(wrap);
    const bounds = wrap.getBoundingClientRect(); edges.setAttribute("width", bounds.width); edges.setAttribute("height", bounds.height);
    const arrowId = `cell-arrow-${host.dataset.cellId || spec.snapshot}`; const defs = document.createElementNS(NS, "defs"); const marker = document.createElementNS(NS, "marker"); marker.setAttribute("id", arrowId); marker.setAttribute("viewBox", "0 0 10 10"); marker.setAttribute("refX", "9"); marker.setAttribute("refY", "5"); marker.setAttribute("markerWidth", "7"); marker.setAttribute("markerHeight", "7"); marker.setAttribute("orient", "auto-start-reverse"); const arrow = document.createElementNS(NS, "polygon"); arrow.setAttribute("points", "0,0 10,5 0,10"); arrow.setAttribute("fill", theme(host).muted); marker.append(arrow); defs.append(marker); edges.append(defs);
    spec.edges.forEach((e) => { const a = nodeEls.get(e.source)?.getBoundingClientRect(); const b = nodeEls.get(e.target)?.getBoundingClientRect(); if (!a || !b) return; const path = document.createElementNS(NS, "path"); const horizontal = wrap.dataset.direction === "horizontal"; const ax = (horizontal ? a.right : a.left + a.width / 2) - bounds.left; const ay = (horizontal ? a.top + a.height / 2 : a.bottom) - bounds.top; const bx = (horizontal ? b.left - 3 : b.left + b.width / 2) - bounds.left; const by = (horizontal ? b.top + b.height / 2 : b.top - 3) - bounds.top; path.setAttribute("marker-end", `url(#${arrowId})`); path.setAttribute("d", horizontal ? `M${ax},${ay} C${(ax + bx) / 2},${ay} ${(ax + bx) / 2},${by} ${bx},${by}` : `M${ax},${ay} C${ax},${(ay + by) / 2} ${bx},${(ay + by) / 2} ${bx},${by}`); const title = document.createElementNS(NS, "title"); title.textContent = e.type; path.append(title); edges.append(path); }); dataDetails(host, "Relationship scope", spec.scope);
  }
  function mount(host, initialSpec, initialOptions = {}) {
    let spec = initialSpec; let options = initialOptions; let frame = null; let destroyed = false; let lastWidth = 0;
    function changeOption(key, callback, value) { options = { ...options, [key]: value }; if (options[callback]) options[callback](value); else schedule(); }
    function draw() {
      frame = null; if (destroyed || host.getBoundingClientRect().width < 1) return; lastWidth = host.getBoundingClientRect().width; host.replaceChildren();
      const interactive = { ...options, onProviderFocus: (value) => changeOption("providerFocus", "onProviderFocus", value), onModelDetail: (value) => changeOption("modelDetail", "onModelDetail", value) };
      ({ line, bar, scatter, share, table, graph }[spec.type] || (() => note(host, "Unsupported view")))(host, spec, interactive);
    }
    function schedule() { if (!frame && !destroyed) frame = requestAnimationFrame(draw); }
    const observer = new ResizeObserver((entries) => { if (Math.abs(entries[0].contentRect.width - lastWidth) > 1) schedule(); }); observer.observe(host); schedule();
    return { update(nextSpec = spec, nextOptions = options) { spec = nextSpec; options = nextOptions; schedule(); }, destroy() { destroyed = true; observer.disconnect(); if (frame) cancelAnimationFrame(frame); host.replaceChildren(); } };
  }
  window.ThothResearchCells = { mount };
})();
