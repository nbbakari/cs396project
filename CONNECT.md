# CONNECT.md — Frontend Integration Guide

A technical contract between the Flask/SQLite backend and the frontend
(Bootstrap 5, Jinja templates, vanilla JavaScript, Chart.js).

**Backend owner:** nbdomain · **Frontend owner:** Clement

Everything below is verified against the code on `main`. If something here
disagrees with the app's behaviour, the document is wrong — say so and it gets
fixed.

---

## 1. Where files go

Flask discovers templates and static assets by convention. A file in the wrong
place will 404.

```
epaData/
├── app/
│   ├── templates/                ← ALL .html files
│   │   ├── base.html             (shared layout, sidebar, flash messages — see §2)
│   │   ├── _macros.html          (pagination, sort headers, badges, download links)
│   │   ├── index.html            /
│   │   ├── retrieval.html        /retrieve
│   │   ├── upload.html           /upload
│   │   ├── review.html           /review/<id>
│   │   ├── explorer.html         /explorer
│   │   ├── compare.html          /compare
│   │   ├── facility_detail.html  /facility/<id>
│   │   ├── unit_detail.html      /unit/<key>
│   │   ├── dataset_detail.html   /dataset/<id>
│   │   └── downloads.html        /downloads
│   ├── static/
│   │   ├── css/app.css           (all custom styles)
│   │   └── js/
│   │       ├── charts.js         (shared Chart.js helpers — see §4)
│   │       ├── explorer.js       (row selection, copy link, top-emitters chart)
│   │       └── upload.js         (drag-and-drop file picker)
│   ├── routes/                   ← backend only
│   ├── services/                 ← backend only
│   └── models.py                 ← backend only
└── run.py
```

### Linking to static files

Always build the URL with `url_for`. Never hardcode `/static/...`.

```html
<link rel="stylesheet" href="{{ url_for('static', filename='css/app.css') }}" />
<script src="{{ url_for('static', filename='js/charts.js') }}"></script>
```

---

## 2. Template inheritance

`base.html` owns the page shell: doctype, `<head>`, Bootstrap 5.3.3 from the
CDN, `app.css`, the sidebar, the top bar, flash messages, and the Bootstrap JS
bundle. **Every page extends it.**

| Block | Purpose | Default |
| --- | --- | --- |
| `title` | Contents of `<title>` | `epaData` |
| `breadcrumb` | Bold text in the top bar | `Overview` |
| `head` | Extra `<link>`/`<meta>` in `<head>` | empty |
| `container_width` | `max-width` of the content column | `1100px` |
| `content` | **Your page body** | empty |
| `scripts` | **Your `<script>` tags**, after the Bootstrap bundle | empty |

```jinja
{% extends "base.html" %}
{% from "_macros.html" import pagination %}

{% block title %}My page &middot; epaData{% endblock %}
{% block breadcrumb %}My page{% endblock %}
{% block container_width %}1200px{% endblock %}

{% block content %}
  <h1 class="h4 mb-1">My page</h1>
  <div class="chart-box"><canvas id="my-chart"></canvas></div>
{% endblock %}

{% block scripts %}
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
  <script src="{{ url_for('static', filename='js/charts.js') }}"></script>
{% endblock %}
```

- **Scripts go in `{% block scripts %}`**, which renders after your markup.
- **Chart.js is not in `base.html`**; pages that draw charts load it themselves.
- **Shared CSS classes** (in `app.css`): `panel`, `panel-title`, `stat-panel`,
  `table-wrap` + `table-data` (with `num` cells for right-aligned numbers),
  `chart-box`, `facts` (definition lists), `nav-card`, `preset-chip`, `query-text`.

### Template helpers

| Helper | Use |
| --- | --- |
| `value \| num` / `num(1)` | Thousands separators, fixed decimals; blank for `None` |
| `value \| when` | Format a datetime (`%Y-%m-%d %H:%M` by default) |
| `text \| fromjson` | Parse a JSON column (e.g. `query_params`) |
| `url_with(page=2)` | Current URL with parameters changed (`None` removes one) |
| `url_for_query('download.records', format='csv')` | Another endpoint with the current query string |
| `sort_url('co2_mass')` | Sort-header link that toggles direction |

### Adding a page

A template alone isn't reachable — it needs a route in `app/routes/` and, for
a top-level page, an entry in `nav_items` in `base.html`.

---

## 3. API data contracts

All endpoints return JSON, including errors (`{"error": ..., "detail": ...}`).

### `GET /api/emissions/records`

The data-explorer search as JSON. Accepts **every** explorer parameter (see
README → *Search reference*): filters (`state`, `year`, `primary_fuel`,
`facility_id`, ...), ranges (`co2_min`, `gross_load_max`, ...), `level`,
`sort`, `order`, `limit`, `group_by`, `per_group`, `page`, `per_page`.

```json
{
  "page": 1, "per_page": 25, "total": 812, "total_pages": 33,
  "level": "unit", "sort": "co2_mass", "order": "desc",
  "records": [
    {
      "facility_id": 6002, "facility_name": "James H Miller Jr",
      "state_code": "AL", "county": "Jefferson County",
      "unit_key": 1234, "epa_unit_id": "1", "unit_type": "Dry bottom wall-fired boiler",
      "primary_fuel": "Coal", "reporting_year": 2025,
      "operating_time": 7812.5, "gross_load": 5120000.0, "heat_input": 50100000.0,
      "co2_mass": 5100000.0, "so2_mass": 1200.4, "nox_mass": 2900.1,
      "so2_control": "Wet Limestone", "dataset_id": 11, "data_source": "CAMPD API"
    }
  ]
}
```

With `level=facility` each record is a facility-year with `unit_count` and
summed metrics; with `group_by` each record also has `group_rank`. Invalid
parameters return `400` with `detail` listing every problem.

### `GET /api/emissions/top-facilities`

Facilities ranked by total CO₂ mass for one year.

| Param | Default | Notes |
| --- | --- | --- |
| `year` | latest year with data | four-digit year; `400` if unparseable |
| `limit` | `10` | clamped to 1–100 |

```json
[{ "facility_id": 6002, "facility_name": "James H Miller Jr", "state_code": "AL",
   "total_co2": 23814854.18, "year": 2022 }]
```

> **Plant names repeat across states** ("Barry"). Key on `facility_id`; label
> as `` `${facility_name} (${state_code})` ``.

Totals are never blended across years. Returns `[]` when there is no data.

### `GET /api/emissions/years`

Every reporting year with records, newest first: `[2025, 2024, ...]`.

---

## 4. Charts

`static/js/charts.js` exposes `window.EpaCharts` with the project's chart
conventions built in: the validated 8-colour categorical palette assigned in
fixed order (never cycled — comparisons are capped at 8 series), 2px lines
with 8px markers, recessive gridlines, compact axis numbers, and hover tooltips.

```js
// One line per entity; legend appears automatically for 2+ series.
EpaCharts.lineChart(canvas, [2023, 2024, 2025],
  [{ label: "Unit 1", data: [1, 2, 3] }, { label: "Unit 2", data: [2, 2, null] }],
  { yTitle: "CO2 (short tons)", unit: "short tons" });

// Single-series horizontal bars (slot 1 colour, no legend).
EpaCharts.barChart(canvas, labels, values, { xTitle: "CO2 (short tons)", unit: "short tons" });
```

**Metric charts without writing JS:** put the data in a JSON script tag and
mark up a container; `charts.js` wires the `<select>` to switch metrics.

```html
<div data-metric-chart="my-data">
  <select data-metric-select><option value="co2_mass">CO2 (short tons)</option></select>
  <div class="chart-box"><canvas></canvas></div>
</div>
<script id="my-data" type="application/json">{{ chart | tojson }}</script>
```

where `chart = {"labels": [years...], "series": {"co2_mass": [values...]}}`.

Rules: one y-axis per chart; text in ink colours, never series colours; a
table view next to any multi-series chart (the compare page has one).

---

## 5. Local setup and Git workflow

Each developer's app stores data in its own SQLite file (`data/epadata.db`,
git-ignored). The team's common data lives in the shared Supabase database
and is copied into the local file once.

```bash
git pull origin main
source .venv/bin/activate     # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
# .env: SHARED_DATABASE_URL=<the team's Supabase connection string>
flask run                     # migrates data/epadata.db on the first request
```

Then open the dashboard and press **Load shared data**. Paste your CAMPD API
key on the **Data Retrieval** page (it saves it to your `.env`). Your own
retrievals and uploads stay on your machine; `flask data push-shared`
publishes them to the team.

`FLASK_DEBUG=1` reloads the server on save, but **template and static changes
need a hard refresh** (Ctrl/Cmd+Shift+R). Run `pytest` before pushing; the
route tests render every page.

---

## Quick reference

| What | Where |
| --- | --- |
| HTML | `app/templates/` |
| CSS / JS | `app/static/css/app.css`, `app/static/js/` |
| Layout to extend | `base.html` |
| Shared macros | `_macros.html` |
| Your markup / scripts | `{% block content %}` / `{% block scripts %}` |
| Search data | `GET /api/emissions/records?<explorer params>` |
| Chart data | `GET /api/emissions/top-facilities?year=YYYY&limit=N` |
| Year list | `GET /api/emissions/years` |
| Charts | Chart.js 4.4.4 CDN + `js/charts.js` |
