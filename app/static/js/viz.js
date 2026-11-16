(function () {
  if (!window.Plotly || !document.getElementById('chart-ts')) return;
  const U = window.VIZ;
  const $ = (id) => document.getElementById(id);
  const GREEN = '#198754';

  const plotConfig = (name) => ({
    responsive: true,
    displaylogo: false,
    modeBarButtonsToRemove: ['lasso2d', 'select2d'],
    toImageButtonOptions: { format: 'png', filename: name, scale: 2 },
  });

  function filterParams() {
    const p = new URLSearchParams();
    [['state', 'f-state'], ['fuel', 'f-fuel'], ['year_min', 'f-ymin'], ['year_max', 'f-ymax']]
      .forEach(([key, id]) => { const v = $(id).value; if (v) p.set(key, v); });
    return p;
  }

  async function getJSON(url, extra) {
    const p = filterParams();
    Object.entries(extra || {}).forEach(([k, v]) => { if (v !== '' && v != null) p.set(k, v); });
    const res = await fetch(url + '?' + p.toString());
    if (!res.ok) throw new Error('Request failed (' + res.status + ')');
    return res.json();
  }

  function message(id, text) {
    const el = $(id);
    Plotly.purge(el);
    el.innerHTML = '<div class="text-muted text-center py-5">' + text + '</div>';
  }

  function draw(id, traces, layout, filename) {
    const el = $(id);
    Plotly.purge(el);
    el.innerHTML = '';
    const base = {
      font: { family: 'inherit' },
      margin: { t: 60, r: 20, b: 60, l: 70 },
      legend: { orientation: 'h', y: -0.25 },
      title: { x: 0.02, xanchor: 'left' },
    };
    Plotly.newPlot(el, traces, Object.assign(base, layout), plotConfig(filename));
  }

  const yearAxis = { title: { text: 'Reporting year' }, dtick: 1, tickformat: 'd' };
  const yearText = (y) => (y ? String(y) : 'all years');

  function pearson(xs, ys) {
    const n = xs.length;
    if (n < 3) return null;
    const mx = xs.reduce((a, b) => a + b, 0) / n;
    const my = ys.reduce((a, b) => a + b, 0) / n;
    let sxx = 0, syy = 0, sxy = 0;
    for (let i = 0; i < n; i++) {
      sxx += (xs[i] - mx) ** 2; syy += (ys[i] - my) ** 2; sxy += (xs[i] - mx) * (ys[i] - my);
    }
    return sxx && syy ? sxy / Math.sqrt(sxx * syy) : null;
  }

  // ---------- Trends: line chart ----------
  async function renderTrends() {
    message('chart-ts', 'Loading…');
    try {
      const d = await getJSON(U.timeseries, { metric: $('ts-metric').value, group: $('ts-group').value });
      if (!d.series.length || !d.series[0].x.length) return message('chart-ts', 'No data for these filters.');
      const traces = d.series.map((s) => ({
        type: 'scatter', mode: 'lines+markers', name: s.name, x: s.x, y: s.y,
        hovertemplate: '<b>' + s.name + '</b><br>Year %{x}<br>' + d.label + ': %{y:,.2f} ' + d.unit + '<extra></extra>',
      }));
      draw('chart-ts', traces, {
        title: { text: d.label + ' over time' },
        xaxis: yearAxis,
        yaxis: { title: { text: d.label + ' (' + d.unit + ')' }, rangemode: 'tozero' },
        hovermode: 'closest',
      }, 'trend-' + d.metric);
    } catch (e) { message('chart-ts', 'Could not load chart: ' + e.message); }
  }

  // ---------- Comparisons: bar chart ----------
  async function renderBar() {
    message('chart-bar', 'Loading…');
    try {
      const d = await getJSON(U.bar, {
        metric: $('bar-metric').value, by: $('bar-by').value,
        year: $('bar-year').value, limit: $('bar-limit').value,
      });
      if (!d.labels.length) return message('chart-bar', 'No data for these filters.');
      const noun = { state: 'states', fuel: 'fuels', facility: 'facilities' }[d.by];
      draw('chart-bar', [{
        type: 'bar', orientation: 'h',
        x: d.values.slice().reverse(), y: d.labels.slice().reverse(),
        marker: { color: GREEN },
        hovertemplate: '%{y}<br>' + d.label + ': %{x:,.2f} ' + d.unit + '<extra></extra>',
      }], {
        title: { text: 'Top ' + d.labels.length + ' ' + noun + ' by ' + d.label + ' (' + yearText(d.year) + ')' },
        xaxis: { title: { text: d.label + ' (' + d.unit + ')' } },
        yaxis: { automargin: true },
        showlegend: false,
        margin: { t: 60, r: 20, b: 60, l: 160 },
      }, 'compare-' + d.by + '-' + d.metric);
    } catch (e) { message('chart-bar', 'Could not load chart: ' + e.message); }
  }

  // ---------- Distributions: histogram + box plot ----------
  async function renderDist() {
    message('chart-hist', 'Loading…'); message('chart-box', 'Loading…');
    try {
      const d = await getJSON(U.distribution, { metric: $('dist-metric').value, year: $('dist-year').value });
      if (!d.values.length) { message('chart-hist', 'No data for these filters.'); return message('chart-box', ''); }
      const logOn = $('dist-log').checked;
      const axisLabel = d.label + ' (' + d.unit + ')';
      const tx = (v) => (logOn ? Math.log10(v) : v);

      draw('chart-hist', [{
        type: 'histogram', x: d.values.map(tx), nbinsx: 40, marker: { color: GREEN },
        hovertemplate: (logOn ? 'log₁₀ range' : 'Range') + ' %{x}<br>Unit-years: %{y}<extra></extra>',
      }], {
        title: { text: 'Distribution of ' + d.label + ' (' + yearText(d.year) + ', n = ' + d.count.toLocaleString() + ')' },
        xaxis: { title: { text: (logOn ? 'log₁₀ of ' : '') + axisLabel } },
        yaxis: { title: { text: 'Number of unit-years' } },
        showlegend: false, bargap: 0.05,
      }, 'histogram-' + d.metric);

      const boxes = Object.entries(d.groups).map(([fuel, vals]) => ({
        type: 'box', name: fuel, y: vals, boxpoints: 'outliers', marker: { size: 3 },
        hovertemplate: fuel + '<br>' + d.label + ': %{y:,.2f} ' + d.unit + '<extra></extra>',
      }));
      draw('chart-box', boxes, {
        title: { text: d.label + ' by primary fuel (' + yearText(d.year) + ')' },
        yaxis: { title: { text: axisLabel }, type: logOn ? 'log' : 'linear' },
        xaxis: { title: { text: 'Primary fuel' } },
      }, 'boxplot-' + d.metric);
    } catch (e) { message('chart-hist', 'Could not load chart: ' + e.message); message('chart-box', ''); }
  }

  // ---------- Relationships: scatter + correlation heat map ----------
  async function renderScatter() {
    message('chart-scatter', 'Loading…');
    try {
      const d = await getJSON(U.scatter, { x: $('sc-x').value, y: $('sc-y').value, year: $('sc-year').value });
      const p = d.points;
      if (!p.x.length) return message('chart-scatter', 'No data for these filters.');
      const groups = {};
      p.fuel.forEach((fuel, i) => {
        if (!groups[fuel]) groups[fuel] = { x: [], y: [], text: [] };
        groups[fuel].x.push(p.x[i]); groups[fuel].y.push(p.y[i]); groups[fuel].text.push(p.name[i]);
      });
      const traces = Object.entries(groups).map(([fuel, g]) => ({
        type: 'scatter', mode: 'markers', name: fuel, x: g.x, y: g.y, text: g.text,
        marker: { size: 6, opacity: 0.65 },
        hovertemplate: '%{text}<br>' + d.x_meta.label + ': %{x:,.2f} ' + d.x_meta.unit +
          '<br>' + d.y_meta.label + ': %{y:,.2f} ' + d.y_meta.unit + '<extra>' + fuel + '</extra>',
      }));
      const r = pearson(p.x, p.y);
      const log = $('sc-log').checked ? 'log' : 'linear';
      draw('chart-scatter', traces, {
        title: { text: d.y_meta.label + ' vs ' + d.x_meta.label + ' (' + yearText(d.year) +
          (r === null ? '' : ', r = ' + r.toFixed(2)) + ', n = ' + p.x.length.toLocaleString() + ')' },
        xaxis: { title: { text: d.x_meta.label + ' (' + d.x_meta.unit + ')' }, type: log },
        yaxis: { title: { text: d.y_meta.label + ' (' + d.y_meta.unit + ')' }, type: log },
        legend: { title: { text: 'Primary fuel' }, orientation: 'h', y: -0.25 },
      }, 'scatter-' + d.x_meta.metric + '-vs-' + d.y_meta.metric);
    } catch (e) { message('chart-scatter', 'Could not load chart: ' + e.message); }
  }

  async function renderHeat() {
    message('chart-heat', 'Loading…');
    try {
      const d = await getJSON(U.correlation, { year: $('heat-year').value });
      if (!d.count) return message('chart-heat', 'Not enough complete records for a correlation matrix.');
      draw('chart-heat', [{
        type: 'heatmap', z: d.matrix, x: d.labels, y: d.labels,
        zmin: -1, zmax: 1, colorscale: 'RdBu', reversescale: true,
        texttemplate: '%{z:.2f}',
        colorbar: { title: { text: 'Pearson r' } },
        hovertemplate: '%{y} vs %{x}<br>Pearson r = %{z:.3f}<extra></extra>',
      }], {
        title: { text: 'Correlation heat map, unit-level metrics (' + yearText(d.year) + ', n = ' + d.count.toLocaleString() + ')' },
        xaxis: { automargin: true },
        yaxis: { automargin: true, autorange: 'reversed' },
        margin: { t: 60, r: 20, b: 80, l: 130 },
      }, 'correlation-heatmap');
    } catch (e) { message('chart-heat', 'Could not load chart: ' + e.message); }
  }

  function renderRelate() { renderScatter(); renderHeat(); }

  // ---------- TRACI impacts ----------
  async function renderTraci() {
    const box = $('traci-charts');
    box.innerHTML = '<div class="text-muted text-center py-5">Loading…</div>';
    try {
      const d = await getJSON(U.traci, {});
      $('traci-note').textContent = d.note || '';
      box.innerHTML = '';
      if (!d.years.length) { box.innerHTML = '<div class="text-muted text-center py-5">No data for these filters.</div>'; return; }
      d.categories.forEach((c) => {
        const div = document.createElement('div');
        div.id = 'chart-traci-' + c.key;
        div.className = 'viz-chart mb-4';
        box.appendChild(div);
        draw(div.id, [{
          type: 'bar', x: d.years, y: c.values, marker: { color: GREEN },
          hovertemplate: 'Year %{x}<br>' + c.label + ': %{y:,.0f} ' + c.unit + '<extra></extra>',
        }], {
          title: { text: c.label + ' by year' },
          xaxis: yearAxis,
          yaxis: { title: { text: c.label + ' (' + c.unit + ')' } },
          showlegend: false,
        }, 'traci-' + c.key);
      });
    } catch (e) { box.innerHTML = '<div class="text-muted text-center py-5">Could not load chart: ' + e.message + '</div>'; }
  }

  // ---------- Tabs, filters and wiring ----------
  const renderers = { trends: renderTrends, compare: renderBar, dist: renderDist, relate: renderRelate, traci: renderTraci };
  const rendered = new Set();
  let active = 'trends';

  function ensure(key) {
    if (!rendered.has(key)) { rendered.add(key); renderers[key](); return; }
    document.querySelectorAll('#pane-' + key + ' .js-plotly-plot').forEach((el) => Plotly.Plots.resize(el));
  }

  document.querySelectorAll('[data-viz]').forEach((btn) => {
    btn.addEventListener('shown.bs.tab', () => { active = btn.dataset.viz; ensure(active); });
  });

  function refreshActive() { rendered.clear(); ensure(active); }
  $('f-apply').addEventListener('click', refreshActive);
  $('f-reset').addEventListener('click', () => {
    ['f-state', 'f-fuel', 'f-ymin', 'f-ymax'].forEach((id) => { $(id).value = ''; });
    refreshActive();
  });

  ['ts-metric', 'ts-group'].forEach((id) => $(id).addEventListener('change', renderTrends));
  ['bar-metric', 'bar-by', 'bar-year', 'bar-limit'].forEach((id) => $(id).addEventListener('change', renderBar));
  ['dist-metric', 'dist-year', 'dist-log'].forEach((id) => $(id).addEventListener('change', renderDist));
  ['sc-x', 'sc-y', 'sc-year', 'sc-log'].forEach((id) => $(id).addEventListener('change', renderScatter));
  $('heat-year').addEventListener('change', renderHeat);

  ensure('trends');
})();