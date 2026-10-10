(function () {
  const forms = Array.from(document.querySelectorAll("[data-table-entry]"));
  if (forms.length === 0) return;

  function parseNumber(raw) {
    if (!raw) return null;
    let s = String(raw).trim().toLowerCase();
    if (!s) return null;
    if (s.endsWith("%")) {
      s = s.slice(0, -1).trim();
    }
    const m = s.match(/^([+-]?\d[\d,]*\.?\d*)([a-z]{1,2})?$/i);
    if (!m) return null;
    const num = parseFloat(m[1].replace(/,/g, ""));
    if (Number.isNaN(num)) return null;
    const suf = (m[2] || "").toLowerCase();
    const mult = { k: 1e3, m: 1e6, mn: 1e6, mm: 1e6, b: 1e9, bn: 1e9, t: 1e12 }[suf];
    return num * (mult || 1);
  }

  function splitLine(line) {
    if (line.includes("\t")) return line.split("\t").map((p) => p.trim()).filter(Boolean);
    const multiSpace = line.trim().split(/\s{2,}/).filter(Boolean);
    if (multiSpace.length > 1) return multiSpace.map((p) => p.trim());
    return line.trim().split(/\s+/).filter(Boolean);
  }

  function parseTable(raw) {
    const lines = String(raw || "")
      .split(/\r?\n/)
      .map((l) => l.trim())
      .filter(Boolean);
    if (lines.length === 0) return null;
    let rows = lines.map(splitLine).filter((r) => r.length > 0);
    if (rows.length === 0) return null;
    let header = null;
    if (rows[0].length >= 2) {
      const headerLabel = String(rows[0][0] || "").toLowerCase();
      const headerByLabel = ["period", "quarter", "qtr", "date", "time", "month", "year", "week"].includes(headerLabel);
      const hasAlpha = rows[0].slice(1).some((cell) => /[a-z]/i.test(cell));
      if (hasAlpha || headerByLabel) {
        header = rows[0];
        rows = rows.slice(1);
      }
    }
    if (rows.length === 0) return null;
    const series = header ? header.slice(1) : rows[0].slice(1).map((_, i) => `series_${i + 1}`);
    const data = rows.map((r) => {
      const period = r[0] || "";
      const values = r.slice(1);
      while (values.length < series.length) values.push("");
      return { period, values };
    });
    return { series, data };
  }

  function escapePreviewHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char] || char));
  }

  function renderTable(previewEl, parsed) {
    if (!previewEl) return;
    if (!parsed) {
      previewEl.innerHTML = "";
      return;
    }
    const header = ["Period"].concat(parsed.series);
    const rows = parsed.data.map((row) => [row.period].concat(row.values));
    let html = "<table class=\"table-preview-table\"><thead><tr>";
    header.forEach((h, idx) => (html += `<th class="${idx === 0 ? "is-date" : "is-numeric"}">${escapePreviewHtml(h)}</th>`));
    html += "</tr></thead><tbody>";
    rows.forEach((r) => {
      html += "<tr>";
      r.forEach((cell, idx) => {
        html += `<td class="${idx === 0 ? "is-date" : "is-numeric"}">${escapePreviewHtml(cell || "")}</td>`;
      });
      html += "</tr>";
    });
    html += "</tbody></table>";
    previewEl.innerHTML = html;
  }

  function renderChart(canvas, parsed) {
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const dpr = window.devicePixelRatio || 1;
    const width = canvas.getAttribute("width") ? Number(canvas.getAttribute("width")) : 520;
    const height = canvas.getAttribute("height") ? Number(canvas.getAttribute("height")) : 220;
    canvas.width = width * dpr;
    canvas.height = height * dpr;
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, width, height);
    if (!parsed || parsed.data.length === 0) return;

    const series = parsed.series;
    const points = series.map(() => []);
    parsed.data.forEach((row) => {
      row.values.forEach((val, sIdx) => {
        const num = parseNumber(val);
        points[sIdx].push(num != null ? num : null);
      });
    });
    const allNums = points.flat().filter((v) => v != null);
    if (allNums.length === 0) return;
    const extent = window.ThothCharting?.axisExtent
      ? window.ThothCharting.axisExtent(allNums)
      : { min: Math.min(...allNums), max: Math.max(...allNums) };
    let min = extent.min;
    let max = extent.max;
    if (min === max) max = min + 1;

    const left = 58;
    const right = 12;
    const top = 16;
    const bottom = 30;
    const plotW = width - left - right;
    const plotH = height - top - bottom;

    const monoFont = getComputedStyle(document.documentElement).getPropertyValue("--mono") || "monospace";
    ctx.font = `10px ${monoFont}`;
    ctx.textBaseline = "middle";
    ctx.fillStyle = "rgba(255,255,255,0.55)";
    ctx.strokeStyle = "rgba(255,255,255,0.08)";
    ctx.lineWidth = 1;
    const ticks = 4;
    for (let i = 0; i <= ticks; i += 1) {
      const y = top + (plotH * i) / ticks;
      const value = max - ((max - min) * i) / ticks;
      ctx.beginPath();
      ctx.moveTo(left, y);
      ctx.lineTo(left + plotW, y);
      ctx.stroke();
      const label = window.ThothCharting?.formatShort ? window.ThothCharting.formatShort(value) : value.toFixed(0);
      ctx.fillText(label, 6, y);
    }

    const colors = ["#6be7ff", "#ffd56b", "#ff6b6b", "#8cff6b", "#c58bff"];
    points.forEach((seriesPoints, sIdx) => {
      ctx.strokeStyle = colors[sIdx % colors.length];
      ctx.lineWidth = 2.4;
      ctx.beginPath();
      let started = false;
      seriesPoints.forEach((val, idx) => {
        if (val == null) return;
        const x = left + (plotW * idx) / Math.max(1, seriesPoints.length - 1);
        const y = top + plotH - ((val - min) / (max - min)) * plotH;
        if (!started) {
          ctx.moveTo(x, y);
          started = true;
        } else {
          ctx.lineTo(x, y);
        }
      });
      ctx.stroke();
    });

    ctx.strokeStyle = "rgba(255,255,255,0.18)";
    ctx.beginPath();
    ctx.moveTo(left, top + plotH);
    ctx.lineTo(left + plotW, top + plotH);
    ctx.stroke();
  }

  function normalizeParsed(parsed) {
    if (!parsed) {
      return { series: ["series_1"], data: [{ period: "", values: [""] }] };
    }
    const series = parsed.series && parsed.series.length ? parsed.series : ["series_1"];
    const data = parsed.data && parsed.data.length ? parsed.data : [{ period: "", values: [] }];
    data.forEach((row) => {
      while (row.values.length < series.length) row.values.push("");
    });
    return { series, data };
  }

  function buildEditor(editorEl, parsed) {
    if (!editorEl) return;
    const thead = editorEl.querySelector("thead");
    const tbody = editorEl.querySelector("tbody");
    if (!thead || !tbody) return;
    const normalized = normalizeParsed(parsed);
    const series = normalized.series;
    const data = normalized.data;
    thead.innerHTML = "";
    tbody.innerHTML = "";

    const headRow = document.createElement("tr");
    const headPeriod = document.createElement("th");
    headPeriod.textContent = "Period";
    headRow.appendChild(headPeriod);
    series.forEach((name, idx) => {
      const th = document.createElement("th");
      const input = document.createElement("input");
      input.type = "text";
      input.value = name || `series_${idx + 1}`;
      input.placeholder = `series_${idx + 1}`;
      input.dataset.seriesName = "1";
      th.appendChild(input);
      headRow.appendChild(th);
    });
    thead.appendChild(headRow);

    data.forEach((row) => {
      const tr = document.createElement("tr");
      const tdPeriod = document.createElement("td");
      const inpPeriod = document.createElement("input");
      inpPeriod.type = "text";
      inpPeriod.value = row.period || "";
      inpPeriod.placeholder = "2025Q1";
      tdPeriod.appendChild(inpPeriod);
      tr.appendChild(tdPeriod);

      const values = row.values || [];
      for (let i = 0; i < series.length; i += 1) {
        const td = document.createElement("td");
        const inp = document.createElement("input");
        inp.type = "text";
        inp.value = values[i] || "";
        if (i === 0) inp.placeholder = "45.0k";
        td.appendChild(inp);
        tr.appendChild(td);
      }
      tbody.appendChild(tr);
    });
  }

  function readEditor(editorEl) {
    const thead = editorEl.querySelector("thead");
    const tbody = editorEl.querySelector("tbody");
    const seriesInputs = Array.from(thead ? thead.querySelectorAll("input[data-series-name]") : []);
    const series = seriesInputs.map((input, idx) => input.value.trim() || `series_${idx + 1}`);
    const rows = Array.from(tbody ? tbody.querySelectorAll("tr") : []).map((tr) => {
      const inputs = Array.from(tr.querySelectorAll("input"));
      const period = inputs[0] ? inputs[0].value : "";
      const values = inputs.slice(1).map((inp) => inp.value);
      while (values.length < series.length) values.push("");
      return { period, values };
    });
    return { series, data: rows.length ? rows : [{ period: "", values: new Array(series.length).fill("") }] };
  }

  function stateToText(state) {
    const series = state.series.map((s, idx) => (s && s.trim() ? s.trim() : `series_${idx + 1}`));
    const lines = [];
    lines.push(["Period"].concat(series).join("\t"));
    state.data.forEach((row) => {
      const values = row.values || [];
      while (values.length < series.length) values.push("");
      const line = [row.period || ""].concat(values.map((v) => v || ""));
      lines.push(line.join("\t"));
    });
    return lines.join("\n");
  }

  forms.forEach((form) => {
    const textarea = form.querySelector("[data-table-data]");
    const preview = form.querySelector("[data-table-preview]");
    const chart = form.querySelector("[data-table-chart]");
    const editor = form.querySelector("[data-table-editor]");
    const addSeriesBtn = form.querySelector("[data-table-add-series]");
    const addRowBtn = form.querySelector("[data-table-add-row]");
    const clearBtn = form.querySelector("[data-table-clear]");
    const scenariosEnabled = (form.dataset.scenariosEnabled || "").toLowerCase() === "true";
    const scenarioLabels = (form.dataset.scenarioLabels || "")
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
    if (!textarea) return;
    let syncing = false;
    const render = () => {
      const parsed = parseTable(textarea.value);
      renderTable(preview, parsed);
      renderChart(chart, parsed);
    };
    const syncFromEditor = () => {
      if (syncing || !editor) return;
      syncing = true;
      const state = readEditor(editor);
      if (!scenariosEnabled) {
        state.series = state.series.slice(0, 1);
        state.data = state.data.map((row) => ({
          period: row.period,
          values: row.values.slice(0, 1),
        }));
      }
      textarea.value = stateToText(state);
      render();
      syncing = false;
    };
    const syncFromTextarea = () => {
      if (syncing || !editor) return;
      syncing = true;
      const parsed = parseTable(textarea.value);
      if (!scenariosEnabled && parsed && parsed.series.length > 1) {
        parsed.series = parsed.series.slice(0, 1);
        parsed.data = parsed.data.map((row) => ({
          period: row.period,
          values: row.values.slice(0, 1),
        }));
      }
      buildEditor(editor, parsed);
      render();
      syncing = false;
    };

    textarea.addEventListener("input", syncFromTextarea);
    if (editor) {
      const parsed = parseTable(textarea.value);
      if (!textarea.value.trim() && scenarioLabels.length) {
        buildEditor(editor, {
          series: scenarioLabels,
          data: [{ period: "", values: new Array(scenarioLabels.length).fill("") }],
        });
        syncFromEditor();
      } else {
        buildEditor(editor, parsed);
        if (!textarea.value.trim()) {
          syncFromEditor();
        } else {
          render();
        }
      }
      editor.addEventListener("input", (event) => {
        if (event.target && event.target.tagName === "INPUT") {
          syncFromEditor();
        }
      });
    } else {
      render();
    }

    if (addSeriesBtn && editor) {
      if (!scenariosEnabled) {
        addSeriesBtn.style.display = "none";
      }
      addSeriesBtn.addEventListener("click", () => {
        const state = readEditor(editor);
        state.series.push(`series_${state.series.length + 1}`);
        state.data.forEach((row) => row.values.push(""));
        buildEditor(editor, state);
        syncFromEditor();
      });
    }
    if (addRowBtn && editor) {
      addRowBtn.addEventListener("click", () => {
        const state = readEditor(editor);
        state.data.push({ period: "", values: new Array(state.series.length).fill("") });
        buildEditor(editor, state);
        syncFromEditor();
      });
    }
    if (clearBtn && editor) {
      clearBtn.addEventListener("click", () => {
        buildEditor(editor, { series: ["series_1"], data: [{ period: "", values: [""] }] });
        syncFromEditor();
      });
    }
  });
})();
