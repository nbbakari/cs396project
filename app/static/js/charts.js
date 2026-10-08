/*
 * Shared Chart.js helpers for epaData.
 *
 * Categorical colors are the validated eight-slot palette, assigned in fixed
 * order by series position and never cycled (comparisons are capped at 8).
 * Single-series charts use slot 1 and no legend; the heading names the series.
 * Text stays in ink colors; only marks carry series color.
 */
(function () {
  if (typeof Chart === "undefined") return;

  const PALETTE = [
    "#2a78d6", "#eb6834", "#1baf7a", "#eda100",
    "#e87ba4", "#008300", "#4a3aa7", "#e34948",
  ];
  const INK = {
    primary: "#0f172a",
    secondary: "#475569",
    grid: "#e2e8f0",
    surface: "#ffffff",
  };

  Chart.defaults.font.family =
    'system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif';
  Chart.defaults.font.size = 12;
  Chart.defaults.color = INK.secondary;

  const number = new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 });
  const compact = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 });

  function axis(title) {
    return {
      beginAtZero: true,
      title: title ? { display: true, text: title, color: INK.secondary } : { display: false },
      grid: { color: INK.grid, drawTicks: false },
      border: { display: false },
      ticks: { padding: 6, callback: (value) => compact.format(value) },
    };
  }

  function tooltip(unit) {
    return {
      backgroundColor: INK.primary,
      titleColor: "#fff",
      bodyColor: "#fff",
      padding: 10,
      callbacks: {
        label: (ctx) => {
          const value = ctx.parsed.y ?? ctx.parsed.x;
          if (value === null || value === undefined) return `${ctx.dataset.label}: no data`;
          const label = ctx.chart.data.datasets.length > 1 ? `${ctx.dataset.label}: ` : "";
          return `${label}${number.format(value)}${unit ? " " + unit : ""}`;
        },
      },
    };
  }

  /** Line chart over years; one dataset per entity, colors by position. */
  function lineChart(canvas, labels, datasets, options) {
    const opts = options || {};
    return new Chart(canvas, {
      type: "line",
      data: {
        labels,
        datasets: datasets.map((d, i) => ({
          label: d.label,
          data: d.data,
          borderColor: PALETTE[i],
          backgroundColor: PALETTE[i],
          borderWidth: 2,
          pointRadius: 4,
          pointHoverRadius: 6,
          pointBorderColor: INK.surface,
          pointBorderWidth: 2,
          tension: 0,
          spanGaps: false,
        })),
      },
      options: {
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: {
            display: datasets.length > 1,
            position: "bottom",
            labels: { color: INK.primary, usePointStyle: true, boxWidth: 8 },
          },
          tooltip: tooltip(opts.unit),
        },
        scales: {
          x: { grid: { display: false }, border: { color: INK.grid }, ticks: { color: INK.secondary } },
          y: axis(opts.yTitle),
        },
      },
    });
  }

  /** Horizontal bar chart of one series (slot 1), largest first. */
  function barChart(canvas, labels, values, options) {
    const opts = options || {};
    return new Chart(canvas, {
      type: "bar",
      data: {
        labels,
        datasets: [{
          label: opts.label || "",
          data: values,
          backgroundColor: PALETTE[0],
          hoverBackgroundColor: "#256abf",
          borderRadius: 4,
          borderSkipped: "start",
          barPercentage: 0.7,
          categoryPercentage: 0.9,
        }],
      },
      options: {
        indexAxis: "y",
        maintainAspectRatio: false,
        onClick: opts.onClick,
        plugins: { legend: { display: false }, tooltip: tooltip(opts.unit) },
        scales: {
          x: axis(opts.xTitle),
          y: { grid: { display: false }, border: { color: INK.grid }, ticks: { color: INK.primary } },
        },
      },
    });
  }

  /*
   * A single-series line chart whose metric is picked from a <select>.
   * Markup: <div data-metric-chart="json-script-id"> with a <canvas> and a
   * <select data-metric-select> inside; the JSON is {labels, series: {metric: [...]}}.
   */
  function initMetricCharts() {
    document.querySelectorAll("[data-metric-chart]").forEach((root) => {
      const source = document.getElementById(root.dataset.metricChart);
      const canvas = root.querySelector("canvas");
      const select = root.querySelector("[data-metric-select]");
      if (!source || !canvas || !select) return;
      const payload = JSON.parse(source.textContent);
      if (!payload.labels.length) return;

      const optionLabel = () => select.options[select.selectedIndex].text;
      const chart = lineChart(canvas, payload.labels, [
        { label: optionLabel(), data: payload.series[select.value] },
      ], { yTitle: optionLabel() });

      select.addEventListener("change", () => {
        chart.data.datasets[0].data = payload.series[select.value];
        chart.data.datasets[0].label = optionLabel();
        chart.options.scales.y.title.text = optionLabel();
        chart.update();
      });
    });
  }

  window.EpaCharts = { PALETTE, lineChart, barChart, number };
  document.addEventListener("DOMContentLoaded", initMetricCharts);
  if (document.readyState !== "loading") initMetricCharts();
})();
