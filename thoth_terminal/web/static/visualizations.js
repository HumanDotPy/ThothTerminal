(function () {
  const dataEl = document.getElementById("charts-data");
  let chartsData = [];
  try {
    chartsData = JSON.parse(dataEl?.textContent || "[]");
  } catch (_e) {
    chartsData = [];
  }
  const Charting = window.ThothCharting;

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" }[char] || char));
  }

  function buildChartOption(chartData) {
    if (Charting?.buildEChartOption) {
      return Charting.buildEChartOption(chartData, { mode: "full", showLegend: true });
    }
    return { xAxis: { type: "category", data: [] }, yAxis: { type: "value" }, series: [] };
  }

  function updateChartStat(chartData) {
    const stat = document.querySelector(`[data-chart-stat="${chartData.id}"]`);
    if (!stat || !Charting?.latestSummary) return;
    const summary = Charting.latestSummary(chartData, { range: "ALL" });
    if (!summary.latest) {
      stat.textContent = "";
      return;
    }
    const value = Charting.formatShort(summary.latest.value, { unit: summary.unit });
    const delta = Number(summary.deltaPct);
    const deltaClass = Number.isFinite(delta) ? (delta >= 0 ? "up" : "down") : "";
    stat.innerHTML = `<span class="dataset-chart-value">${escapeHtml(value)}</span><span class="dataset-chart-date">${escapeHtml(String(summary.latest.date || "latest"))}</span>${Number.isFinite(delta) ? `<span class="dataset-chart-delta ${deltaClass}">${delta >= 0 ? "+" : ""}${delta.toFixed(1)}%</span>` : ""}`;
  }

  function initChart(chartData) {
    const container = document.getElementById("chart-" + chartData.id);
    if (!container || container._echartsInstance || !window.echarts) return;
    const chart = echarts.init(container, null, { renderer: "canvas" });
    chart.setOption(buildChartOption(chartData));
    container._echartsInstance = chart;
  }

  function initCharts() {
    const chartById = new Map(chartsData.map((chartData) => [String(chartData.id), chartData]));
    const containers = chartsData
      .map((chartData) => {
        updateChartStat(chartData);
        return document.getElementById("chart-" + chartData.id);
      })
      .filter(Boolean);

    if (!("IntersectionObserver" in window)) {
      chartsData.forEach(initChart);
      return;
    }

    const observer = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        const chartId = String(entry.target.id || "").replace(/^chart-/, "");
        const chartData = chartById.get(chartId);
        if (chartData) initChart(chartData);
        observer.unobserve(entry.target);
      });
    }, { rootMargin: "320px 0px" });

    containers.forEach((container) => {
      observer.observe(container);
    });
  }

  const overlay = document.getElementById("viz-fullscreen-overlay");
  const fsTitle = document.getElementById("viz-fullscreen-title");
  const fsChart = document.getElementById("viz-fullscreen-chart");
  const fsClose = document.getElementById("viz-fullscreen-close");
  let fsInstance = null;

  document.querySelectorAll(".viz-fullscreen-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const chartId = btn.dataset.chartId;
      const chartData = chartsData.find((chart) => String(chart.id) === chartId);
      if (!chartData || !overlay || !fsTitle || !fsChart || !window.echarts) return;

      fsTitle.textContent = chartData.title;
      overlay.style.display = "flex";

      setTimeout(() => {
        if (fsInstance) fsInstance.dispose();
        fsInstance = echarts.init(fsChart, null, { renderer: "canvas" });
        fsInstance.setOption(buildChartOption(chartData));
      }, 50);
    });
  });

  if (fsClose) {
    fsClose.addEventListener("click", () => {
      if (overlay) overlay.style.display = "none";
      if (fsInstance) fsInstance.dispose();
      fsInstance = null;
    });
  }

  overlay?.addEventListener("click", (e) => {
    if (e.target === overlay) {
      overlay.style.display = "none";
      if (fsInstance) fsInstance.dispose();
      fsInstance = null;
    }
  });

  document.querySelectorAll(".viz-filters .filter-chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      const category = chip.dataset.category;
      document.querySelectorAll(".viz-filters .filter-chip").forEach((item) => item.classList.remove("active"));
      chip.classList.add("active");

      document.querySelectorAll(".viz-card").forEach((card) => {
        if (!category || card.dataset.category === category) {
          card.style.display = "";
        } else {
          card.style.display = "none";
        }
      });
    });
  });

  window.addEventListener("resize", () => {
    chartsData.forEach((chartData) => {
      const container = document.getElementById("chart-" + chartData.id);
      if (container?._echartsInstance) {
        container._echartsInstance.resize();
      }
    });
    if (fsInstance) fsInstance.resize();
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initCharts);
  } else {
    initCharts();
  }
})();
