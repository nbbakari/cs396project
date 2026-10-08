/*
 * Data explorer enhancements. The search itself is a plain GET form rendered
 * by the server, so the page works without this script; it adds the row
 * selection controls, "copy link", and the top-emitters chart.
 */
(function () {
  // --- Row selection ------------------------------------------------------
  const selectAll = document.getElementById("select-all");
  const boxes = Array.from(document.querySelectorAll(".row-select"));
  const actions = document.querySelectorAll(".selection-action");
  const countEl = document.getElementById("selection-count");

  function refreshSelection() {
    const selected = boxes.filter((b) => b.checked).length;
    actions.forEach((button) => (button.disabled = selected === 0));
    countEl.textContent = selected ? `${selected} selected:` : "Select rows to";
    if (selectAll) {
      selectAll.checked = selected > 0 && selected === boxes.length;
      selectAll.indeterminate = selected > 0 && selected < boxes.length;
    }
  }
  boxes.forEach((box) => box.addEventListener("change", refreshSelection));
  if (selectAll) {
    selectAll.addEventListener("change", () => {
      boxes.forEach((box) => (box.checked = selectAll.checked));
      refreshSelection();
    });
  }
  refreshSelection();

  // --- Copy link --------------------------------------------------------------
  const copyButton = document.getElementById("copy-link");
  if (copyButton) {
    copyButton.addEventListener("click", () => {
      const done = () => {
        copyButton.textContent = "Link copied";
        setTimeout(() => (copyButton.textContent = "Copy link"), 2000);
      };
      if (navigator.clipboard) {
        navigator.clipboard.writeText(window.location.href).then(done, () => window.prompt("Copy this link:", window.location.href));
      } else {
        window.prompt("Copy this link:", window.location.href);
      }
    });
  }

  // --- Top-emitters chart -----------------------------------------------------
  const urls = window.EXPLORER_URLS;
  const yearSelect = document.getElementById("chart-year");
  const canvas = document.getElementById("co2-chart");
  const statusEl = document.getElementById("chart-status");
  if (!urls || !yearSelect || !canvas || !window.EpaCharts) return;

  let chart = null;
  let facilityIds = [];

  function facilityUrl(id) {
    return urls.facilityTemplate.replace(/\/0$/, "/" + id);
  }

  function draw(rows) {
    // Plant names repeat across states ("Barry"), so labels carry the state.
    const labels = rows.map((r) => `${r.facility_name} (${r.state_code || "?"})`);
    const values = rows.map((r) => r.total_co2);
    facilityIds = rows.map((r) => r.facility_id);
    if (chart) {
      chart.data.labels = labels;
      chart.data.datasets[0].data = values;
      chart.update();
      return;
    }
    chart = window.EpaCharts.barChart(canvas, labels, values, {
      label: "Total CO2 mass",
      unit: "short tons",
      xTitle: "CO₂ mass (short tons)",
      onClick: (_event, elements) => {
        if (elements.length) window.location.href = facilityUrl(facilityIds[elements[0].index]);
      },
    });
  }

  function loadChart() {
    statusEl.textContent = "Loading…";
    const params = new URLSearchParams({ limit: 10 });
    if (yearSelect.value) params.set("year", yearSelect.value);
    fetch(`${urls.topFacilities}?${params}`)
      .then((response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.json();
      })
      .then((rows) => {
        if (!rows.length) {
          statusEl.textContent = "No emissions data for this year.";
          draw([]);
          return;
        }
        draw(rows);
        statusEl.textContent = `Top ${rows.length} facilities for ${rows[0].year}. Click a bar to open the facility.`;
      })
      .catch((error) => {
        console.error("Failed to load chart data:", error);
        statusEl.textContent = `Could not load chart data: ${error.message}`;
      });
  }

  fetch(urls.years)
    .then((response) => response.json())
    .then((years) => {
      years.forEach((year) => {
        const option = document.createElement("option");
        option.value = year;
        option.textContent = year;
        yearSelect.appendChild(option);
      });
    })
    .catch((error) => console.error("Failed to load years:", error))
    .finally(loadChart);

  yearSelect.addEventListener("change", loadChart);
})();
