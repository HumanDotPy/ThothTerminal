(function () {
  const charts = Array.from(document.querySelectorAll("[data-sparkline]"));
  if (!charts.length) return;

  charts.forEach((canvas) => {
    const raw = canvas.getAttribute("data-values") || "";
    const values = raw
      .split(",")
      .map((v) => Number(v))
      .filter((v) => !Number.isNaN(v));
    if (values.length < 2) return;

    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const dpr = window.devicePixelRatio || 1;
    const width = canvas.getAttribute("width") ? Number(canvas.getAttribute("width")) : 420;
    const height = canvas.getAttribute("height") ? Number(canvas.getAttribute("height")) : 90;
    canvas.width = width * dpr;
    canvas.height = height * dpr;
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, width, height);

    const min = Math.min(...values);
    const max = Math.max(...values);
    const pad = (max - min || 1) * 0.12;
    const lo = min - pad;
    const hi = max + pad;

    const left = 8;
    const right = 8;
    const top = 10;
    const bottom = 10;
    const plotW = width - left - right;
    const plotH = height - top - bottom;

    const points = values.map((v, idx) => {
      const x = left + (plotW * idx) / (values.length - 1);
      const y = top + plotH - ((v - lo) / (hi - lo)) * plotH;
      return [x, y];
    });

    ctx.strokeStyle = "rgba(255,255,255,0.15)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(left, top + plotH);
    ctx.lineTo(left + plotW, top + plotH);
    ctx.stroke();

    const grad = ctx.createLinearGradient(0, top, 0, top + plotH);
    grad.addColorStop(0, "rgba(255,176,32,0.4)");
    grad.addColorStop(1, "rgba(255,176,32,0)");

    ctx.beginPath();
    points.forEach(([x, y], idx) => {
      if (idx === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.strokeStyle = "rgba(255,176,32,0.9)";
    ctx.lineWidth = 2;
    ctx.stroke();

    ctx.lineTo(left + plotW, top + plotH);
    ctx.lineTo(left, top + plotH);
    ctx.closePath();
    ctx.fillStyle = grad;
    ctx.fill();
  });
})();
