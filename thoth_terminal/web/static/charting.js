(function () {
  const COLORS = {
    primary: "#ffb020",
    blue: "#47a3ff",
    green: "#22c55e",
    red: "#ef4444",
    purple: "#a855f7",
    cyan: "#6be7ff",
    grid: "rgba(255,255,255,0.075)",
    text: "rgba(255,255,255,0.74)",
    textMuted: "rgba(255,255,255,0.48)",
    axis: "rgba(255,255,255,0.18)",
  };

  const PALETTE = [COLORS.primary, COLORS.blue, COLORS.green, COLORS.purple, COLORS.cyan, COLORS.red];
  const MONO_FONT = '"SF Mono", "Fira Code", "JetBrains Mono", Monaco, Consolas, monospace';

  function finiteNumber(value) {
    const n = Number(value);
    return Number.isFinite(n) ? n : null;
  }

  function trimNumber(value, digits) {
    const n = Number(value);
    if (!Number.isFinite(n)) return "";
    return n.toFixed(digits).replace(/\.0+$/, "").replace(/(\.\d*?)0+$/, "$1");
  }

  function formatShort(value, options) {
    const opts = options || {};
    const n = finiteNumber(value);
    if (n === null) return String(value ?? "");
    const unit = String(opts.unit || "").trim();
    const abs = Math.abs(n);
    const sign = n < 0 ? "-" : "";
    const suffixes = [
      [1000000000000, "T"],
      [1000000000, "B"],
      [1000000, "M"],
      [1000, "K"],
    ];
    for (const item of suffixes) {
      const divisor = item[0];
      const suffix = item[1];
      if (abs >= divisor) {
        const scaled = abs / divisor;
        const digits = scaled >= 100 ? 0 : scaled >= 10 ? 1 : 2;
        return `${sign}${trimNumber(scaled, digits)}${suffix}${unit && unit === "%" ? "%" : ""}`;
      }
    }
    const digits = Number.isInteger(n) ? 0 : abs >= 100 ? 1 : 2;
    const rendered = n.toLocaleString(undefined, { maximumFractionDigits: digits });
    return unit === "%" ? `${rendered}%` : rendered;
  }

  function formatFull(value, options) {
    const opts = options || {};
    const n = finiteNumber(value);
    if (n === null) return String(value ?? "");
    const unit = String(opts.unit || "").trim();
    const rendered = n.toLocaleString(undefined, { maximumFractionDigits: 2 });
    return unit === "%" ? `${rendered}%` : rendered;
  }

  function parseDate(value) {
    if (value instanceof Date && Number.isFinite(value.getTime())) return value;
    const raw = String(value || "").trim();
    if (!raw) return null;
    const parsed = new Date(raw);
    return Number.isFinite(parsed.getTime()) ? parsed : null;
  }

  function formatDateLabel(value) {
    const date = parseDate(value);
    if (!date) return String(value ?? "");
    const month = date.toLocaleString(undefined, { month: "short" });
    return date.getMonth() === 0 ? `${month} ${date.getFullYear()}` : month;
  }

  function formatDateFull(value) {
    const date = parseDate(value);
    if (!date) return String(value ?? "");
    return date.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "2-digit" });
  }

  function sortPoints(points) {
    return (Array.isArray(points) ? points : []).slice().sort((a, b) => {
      const ad = parseDate(a.date);
      const bd = parseDate(b.date);
      if (ad && bd) return ad.getTime() - bd.getTime();
      return String(a.date || "").localeCompare(String(b.date || ""));
    });
  }

  function seriesForRange(points, range) {
    const sorted = sortPoints(points);
    if (!sorted.length || range === "ALL") return sorted;
    const dated = sorted.map((point) => ({ point, date: parseDate(point.date) })).filter((item) => item.date);
    if (!dated.length) return sorted;
    const latest = dated[dated.length - 1].date;
    const cutoff = new Date(latest.getTime());
    if (range === "YTD") {
      cutoff.setMonth(0, 1);
      cutoff.setHours(0, 0, 0, 0);
    } else {
      const months = { "6M": 6, "1Y": 12, "2Y": 24, "5Y": 60, "10Y": 120 }[range] || 60;
      cutoff.setMonth(cutoff.getMonth() - months);
    }
    return dated.filter((item) => item.date >= cutoff).map((item) => item.point);
  }

  function normalizeSeriesList(chartData, range) {
    const raw = chartData || {};
    const sourceSeries = Array.isArray(raw.series) ? raw.series : [];
    if (sourceSeries.length && Array.isArray(sourceSeries[0]?.data)) {
      return sourceSeries.map((series, index) => ({
        name: series.name || raw.title || `Series ${index + 1}`,
        unit: series.unit || raw.unit || "",
        display_type: series.display_type || series.chart_type || raw.display_type || raw.chart_style || "line",
        y_axis_index: Number(series.y_axis_index || 0),
        stack_group: series.stack_group || undefined,
        color: series.color || PALETTE[index % PALETTE.length],
        data: seriesForRange(series.data || [], range || "ALL").map((point) => ({
          date: point.date,
          value: finiteNumber(point.value),
          is_estimate: Boolean(point.is_estimate),
        })),
      }));
    }
    return [
      {
        name: raw.series_label || raw.title || "Metric",
        unit: raw.unit || "",
        display_type: raw.display_type || "line",
        y_axis_index: 0,
        stack_group: undefined,
        color: raw.color || COLORS.blue,
        data: seriesForRange(sourceSeries, range || "ALL").map((point) => ({
          date: point.date,
          value: finiteNumber(point.value),
          is_estimate: Boolean(point.is_estimate),
        })),
      },
    ];
  }

  function niceNumber(range, round) {
    if (!Number.isFinite(range) || range <= 0) return 1;
    const exponent = Math.floor(Math.log10(range));
    const fraction = range / Math.pow(10, exponent);
    let niceFraction = 10;
    if (round) {
      if (fraction < 1.5) niceFraction = 1;
      else if (fraction < 2.25) niceFraction = 2;
      else if (fraction < 3.5) niceFraction = 2.5;
      else if (fraction < 7.5) niceFraction = 5;
      else niceFraction = 10;
    } else if (fraction <= 1) niceFraction = 1;
    else if (fraction <= 2) niceFraction = 2;
    else if (fraction <= 2.5) niceFraction = 2.5;
    else if (fraction <= 5) niceFraction = 5;
    return niceFraction * Math.pow(10, exponent);
  }

  function axisExtent(values, options) {
    const opts = options || {};
    const nums = values.map(finiteNumber).filter((value) => value !== null);
    if (!nums.length) return {};
    let min = Math.min(...nums);
    let max = Math.max(...nums);
    if (min === max) {
      const delta = Math.max(Math.abs(max) * 0.08, 1);
      min -= delta;
      max += delta;
    }
    const includeZero = Boolean(opts.includeZero) || (min < 0 && max > 0);
    if (includeZero) {
      min = Math.min(min, 0);
      max = Math.max(max, 0);
    }
    const span = Math.max(max - min, 1);
    const paddedMin = includeZero && min === 0 ? 0 : min - span * 0.1;
    const paddedMax = includeZero && max === 0 ? 0 : max + span * 0.1;
    const step = niceNumber((paddedMax - paddedMin) / 4, true);
    return {
      min: Math.floor(paddedMin / step) * step,
      max: Math.ceil(paddedMax / step) * step,
      interval: step,
    };
  }

  function collectDates(seriesList) {
    const set = new Set();
    seriesList.forEach((series) => {
      (series.data || []).forEach((point) => {
        if (point.date !== null && point.date !== undefined && String(point.date).trim()) set.add(String(point.date));
      });
    });
    return Array.from(set).sort((a, b) => {
      const ad = parseDate(a);
      const bd = parseDate(b);
      if (ad && bd) return ad.getTime() - bd.getTime();
      return a.localeCompare(b);
    });
  }

  function buildEChartOption(chartData, options) {
    const opts = options || {};
    const range = opts.range || "ALL";
    const seriesList = normalizeSeriesList(chartData, range).filter((series) => (series.data || []).some((point) => point.value !== null));
    const dates = collectDates(seriesList);
    const hasRightAxis = seriesList.some((series) => Number(series.y_axis_index || 0) === 1);
    const hasBar = seriesList.some((series) => String(series.display_type || "").includes("bar"));
    const compact = opts.mode === "compact";

    const eSeries = seriesList.map((series, index) => {
      const dataMap = new Map((series.data || []).map((point) => [String(point.date), point.value]));
      const displayType = String(series.display_type || "line");
      const color = series.color || PALETTE[index % PALETTE.length];
      const isBar = displayType.includes("bar");
      const isArea = displayType === "area";
      return {
        name: series.name,
        type: isBar ? "bar" : "line",
        yAxisIndex: Number(series.y_axis_index || 0),
        stack: series.stack_group || undefined,
        smooth: !isBar,
        symbol: "none",
        sampling: "lttb",
        lineStyle: { width: compact ? 2.2 : 2.5, color },
        areaStyle: isArea ? { opacity: compact ? 0.08 : 0.14, color } : undefined,
        itemStyle: { color, borderRadius: isBar ? [3, 3, 0, 0] : 0 },
        barMaxWidth: 34,
        data: dates.map((date) => dataMap.get(String(date)) ?? null),
      };
    });

    const leftValues = [];
    const rightValues = [];
    seriesList.forEach((series) => {
      const target = Number(series.y_axis_index || 0) === 1 ? rightValues : leftValues;
      (series.data || []).forEach((point) => {
        if (point.value !== null) target.push(point.value);
      });
    });
    const leftExtent = axisExtent(leftValues, { includeZero: hasBar || Boolean(opts.includeZero) });
    const rightExtent = axisExtent(rightValues, { includeZero: Boolean(opts.includeRightZero) });
    const leftUnit = chartData?.unit || seriesList.find((series) => Number(series.y_axis_index || 0) !== 1)?.unit || "";
    const rightUnit = seriesList.find((series) => Number(series.y_axis_index || 0) === 1)?.unit || "";

    const axisLabel = (unit) => ({
      color: COLORS.text,
      fontFamily: MONO_FONT,
      fontSize: compact ? 10 : 11,
      formatter: (value) => formatShort(value, { unit }),
    });

    const yAxes = [
      {
        type: "value",
        name: opts.yLabel || chartData?.y_label || leftUnit || "",
        nameGap: 10,
        nameTextStyle: { color: COLORS.textMuted, fontFamily: MONO_FONT, fontSize: compact ? 10 : 11, align: "left" },
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: axisLabel(leftUnit),
        splitLine: { lineStyle: { color: COLORS.grid } },
        splitNumber: 4,
        scale: !Boolean(opts.includeZero),
        ...leftExtent,
      },
    ];
    if (hasRightAxis) {
      yAxes.push({
        type: "value",
        name: rightUnit || "",
        position: "right",
        nameGap: 10,
        nameTextStyle: { color: COLORS.textMuted, fontFamily: MONO_FONT, fontSize: compact ? 10 : 11, align: "right" },
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: axisLabel(rightUnit),
        splitLine: { show: false },
        splitNumber: 4,
        scale: true,
        ...rightExtent,
      });
    }

    return {
      backgroundColor: "transparent",
      color: PALETTE,
      tooltip: {
        trigger: "axis",
        backgroundColor: "rgba(7,10,13,0.97)",
        borderColor: "rgba(255,255,255,0.12)",
        borderWidth: 1,
        padding: [10, 12],
        textStyle: { color: COLORS.text, fontFamily: MONO_FONT, fontSize: 12 },
        axisPointer: { type: "line", lineStyle: { color: COLORS.axis, width: 1 } },
        formatter: (params) => {
          const rows = Array.isArray(params) ? params : [params];
          const dateLabel = formatDateFull(rows[0]?.axisValue);
          const body = rows
            .map((row) => {
              const series = seriesList.find((item) => item.name === row.seriesName) || {};
              return `<div style="display:flex;gap:18px;justify-content:space-between;align-items:center;"><span>${row.marker || ""}${row.seriesName}</span><strong>${formatFull(row.value, { unit: series.unit })}</strong></div>`;
            })
            .join("");
          return `<div style="margin-bottom:6px;color:${COLORS.textMuted};">${dateLabel}</div>${body}`;
        },
      },
      legend: {
        show: opts.showLegend !== false && eSeries.length > 1,
        top: 0,
        left: "center",
        textStyle: { color: COLORS.text, fontFamily: MONO_FONT, fontSize: 11 },
        itemWidth: 16,
        itemHeight: 8,
      },
      grid: {
        left: compact ? 54 : 68,
        right: hasRightAxis ? 62 : compact ? 18 : 26,
        top: eSeries.length > 1 && opts.showLegend !== false ? 42 : compact ? 18 : 24,
        bottom: compact ? 30 : 38,
        containLabel: false,
      },
      xAxis: {
        type: "category",
        data: dates,
        boundaryGap: hasBar,
        axisLine: { lineStyle: { color: COLORS.axis } },
        axisTick: { show: false },
        axisLabel: {
          color: COLORS.textMuted,
          fontFamily: MONO_FONT,
          fontSize: compact ? 10 : 11,
          hideOverlap: true,
          rotate: !compact && dates.length > 24 ? 35 : 0,
          formatter: formatDateLabel,
        },
      },
      yAxis: yAxes,
      series: eSeries,
      ...(chartData?.config || {}),
    };
  }

  function latestSummary(chartData, options) {
    const series = normalizeSeriesList(chartData || {}, options?.range || "ALL")[0] || { data: [] };
    const points = sortPoints(series.data || []).filter((point) => point.value !== null);
    if (!points.length) return { latest: null, previous: null, deltaPct: null, range: "", count: 0 };
    const latest = points[points.length - 1];
    const previous = points.length > 1 ? points[points.length - 2] : null;
    const deltaPct = previous && previous.value !== 0 ? ((latest.value - previous.value) / Math.abs(previous.value)) * 100 : null;
    const range = points.length > 1 ? `${formatDateLabel(points[0].date)} to ${formatDateLabel(latest.date)}` : formatDateLabel(latest.date);
    return { latest, previous, deltaPct, range, count: points.length, unit: series.unit || chartData?.unit || "" };
  }

  window.ThothCharting = {
    colors: COLORS,
    palette: PALETTE,
    formatShort,
    formatFull,
    formatDateLabel,
    formatDateFull,
    finiteNumber,
    seriesForRange,
    normalizeSeriesList,
    axisExtent,
    buildEChartOption,
    latestSummary,
  };
})();
