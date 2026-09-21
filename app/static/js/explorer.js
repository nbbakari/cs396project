(function () {
  const state = {
    page: 1,
    perPage: 25,
    sort: "reporting_year",
    order: "desc",
    year: "",
    state: "",
  };

  const tbody = document.getElementById("records-body");
  const summaryEl = document.getElementById("result-summary");
  const pageSummaryEl = document.getElementById("page-summary");
  const prevBtn = document.getElementById("page-prev");
  const nextBtn = document.getElementById("page-next");
  const yearSelect = document.getElementById("filter-year");
  const stateInput = document.getElementById("filter-state");
  const applyBtn = document.getElementById("filter-apply");
  const resetBtn = document.getElementById("filter-reset");
  const headers = document.querySelectorAll("#records-table th.sortable");

  if (!tbody) return;

  function facilityUrl(id) {
    return window.EXPLORER_URLS.facilityTemplate.replace(/\/0$/, "/" + id);
  }
  function unitUrl(id) {
    return window.EXPLORER_URLS.unitTemplate.replace(/\/0$/, "/" + id);
  }

  function updateHeaderIndicators() {
    headers.forEach((th) => {
      const key = th.dataset.sort;
      th.classList.toggle("active", key === state.sort);
      let arrow = th.querySelector(".arrow");
      if (!arrow) {
        arrow = document.createElement("span");
        arrow.className = "arrow";
        th.appendChild(arrow);
      }
      arrow.textContent = key === state.sort ? (state.order === "asc" ? "▲" : "▼") : "↕";
    });
  }

  function renderRows(records) {
    if (!records.length) {
      tbody.innerHTML =
        '<tr><td colspan="9" class="text-center text-body-secondary py-4">No records match these filters.</td></tr>';
      return;
    }
    tbody.innerHTML = records
      .map(
        (r) => `
      <tr>
        <td><a class="row-link" href="${facilityUrl(r.facility_id)}">${r.facility_name}</a></td>
        <td>${r.state_code || ""}</td>
        <td><a class="row-link" href="${unitUrl(r.unit_key)}">${r.epa_unit_id}</a></td>
        <td>${r.reporting_year}</td>
        <td class="text-end">${r.gross_load.toLocaleString()}</td>
        <td class="text-end">${r.heat_input.toLocaleString()}</td>
        <td class="text-end">${r.co2_mass.toLocaleString()}</td>
        <td class="text-end">${r.so2_mass.toLocaleString()}</td>
        <td class="text-end">${r.nox_mass.toLocaleString()}</td>
      </tr>`
      )
      .join("");
  }

  function load() {
    tbody.innerHTML =
      '<tr><td colspan="9" class="text-center text-body-secondary py-4">Loading&hellip;</td></tr>';

    const params = new URLSearchParams({
      page: state.page,
      per_page: state.perPage,
      sort: state.sort,
      order: state.order,
    });
    if (state.year) params.set("year", state.year);
    if (state.state) params.set("state", state.state);

    fetch(`${window.EXPLORER_URLS.records}?${params.toString()}`)
      .then((response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status} ${response.statusText}`);
        return response.json();
      })
      .then((data) => {
        renderRows(data.records);
        summaryEl.textContent = `${data.total.toLocaleString()} record(s)`;
        pageSummaryEl.textContent = `Page ${data.page} of ${data.total_pages}`;
        prevBtn.disabled = data.page <= 1;
        nextBtn.disabled = data.page >= data.total_pages;
        updateHeaderIndicators();
      })
      .catch((error) => {
        console.error("Failed to load records:", error);
        tbody.innerHTML = `<tr><td colspan="9" class="text-center text-danger py-4">Could not load data: ${error.message}</td></tr>`;
      });
  }

  function loadYears() {
    fetch(window.EXPLORER_URLS.years)
      .then((response) => response.json())
      .then((years) => {
        years.forEach((year) => {
          const opt = document.createElement("option");
          opt.value = year;
          opt.textContent = year;
          yearSelect.appendChild(opt);
        });
      })
      .catch((error) => console.error("Failed to load years:", error));
  }

  headers.forEach((th) => {
    th.addEventListener("click", () => {
      const key = th.dataset.sort;
      if (state.sort === key) {
        state.order = state.order === "asc" ? "desc" : "asc";
      } else {
        state.sort = key;
        state.order = "desc";
      }
      state.page = 1;
      load();
    });
  });

  prevBtn.addEventListener("click", () => {
    if (state.page > 1) {
      state.page -= 1;
      load();
    }
  });
  nextBtn.addEventListener("click", () => {
    state.page += 1;
    load();
  });

  applyBtn.addEventListener("click", () => {
    state.year = yearSelect.value;
    state.state = stateInput.value.trim().toUpperCase();
    state.page = 1;
    load();
  });
  resetBtn.addEventListener("click", () => {
    yearSelect.value = "";
    stateInput.value = "";
    state.year = "";
    state.state = "";
    state.page = 1;
    load();
  });

  loadYears();
  load();
})();