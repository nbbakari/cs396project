# CONNECT.md — Frontend Integration Guide

A technical contract between the Flask/PostgreSQL backend and the frontend
(Bootstrap 5, vanilla JavaScript, Plotly).

**Backend owner:** nbdomain · **Frontend owner:** Clement

Everything below is verified against the code on `main`. If something here
disagrees with the app's behaviour, the document is wrong — say so and it gets
fixed.

---

## 1. Where files go

Flask discovers templates and static assets by convention. These two locations
are not configurable in this project — a file in the wrong place will 404.

```
epaData/
├── app/
│   ├── templates/        ← ALL .html files
│   │   ├── base.html         (shared layout + navbar — see §2)
│   │   ├── index.html
│   │   ├── upload.html
│   │   └── explorer.html
│   ├── static/           ← ALL .css, .js, images
│   │   ├── css/
│   │   ├── js/
│   │   └── img/
│   ├── routes/           ← backend only
│   └── models.py         ← backend only
└── run.py
```

### Linking to static files

Always build the URL with `url_for`. Never hardcode `/static/...` — if the app
is ever mounted under a path prefix, hardcoded URLs break and `url_for` does
not.

```html
<link rel="stylesheet" href="{{ url_for('static', filename='css/charts.css') }}" />
<script src="{{ url_for('static', filename='js/explorer.js') }}"></script>
<img src="{{ url_for('static', filename='img/logo.png') }}" alt="epaData" />
```

`app/static/css/`, `app/static/js/` and `app/static/img/` already exist (each
holds a `.gitkeep`). Drop files straight in.

---

## 2. Template inheritance

`app/templates/base.html` owns the page shell: `<!doctype>`, `<head>`, the
Bootstrap 5.3.3 CDN links, the top navbar, the flash-message area, and the
Bootstrap JS bundle. **Every page extends it.** Don't repeat the doctype, the
`<head>`, or the Bootstrap tags in a child template — you'd load Bootstrap
twice.

### The five blocks

| Block | Purpose | Default if you omit it |
| --- | --- | --- |
| `title` | Contents of `<title>` | `epaData` |
| `head` | Extra `<link>`/`<meta>` inside `<head>` | empty |
| `content` | **Your page body.** Goes inside the `<main>` container. | empty |
| `container_width` | `max-width` of the `<main>` container | `720px` |
| `scripts` | **Your `<script>` tags.** Rendered after the Bootstrap bundle. | empty |

### Skeleton

```jinja
{% extends "base.html" %}

{% block title %}Data explorer &middot; epaData{% endblock %}

{% block container_width %}960px{% endblock %}

{% block content %}
  <h1 class="h3 mb-4">Data explorer</h1>
  <div id="co2-chart"></div>
{% endblock %}

{% block scripts %}
  <script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
  <script src="{{ url_for('static', filename='js/explorer.js') }}"></script>
{% endblock %}
```

Two things that matter here:

- **Put scripts in `{% block scripts %}`, not in `{% block content %}`.** The
  `scripts` block renders at the very end of `<body>`, after Bootstrap's bundle
  and after your markup exists in the DOM. A script inside `content` runs before
  the rest of the page is parsed, so `document.getElementById("co2-chart")`
  returns `null`.
- **Plotly is not loaded yet.** `base.html` pulls in Bootstrap only. Add the
  Plotly CDN tag yourself, in the `scripts` block of the pages that need it
  (as above), or in `{% block head %}` if you prefer it to load earlier.

### Adding a new page

A template alone isn't reachable — Flask needs a route pointing at it. Ask for
one and it gets added to `app/routes/main.py`, plus a navbar entry in
`base.html`. Currently routed: `/` → `index.html`, `/upload` → `upload.html`,
`/explorer` → `explorer.html`.

---

## 3. API data contracts

Two live endpoints. Both always return JSON — including on error — so `fetch`
never has to parse an HTML error page.

### `GET /api/emissions/top-facilities`

Facilities ranked by total CO₂ mass for a single reporting year.

**Query parameters**

| Param | Required | Default | Notes |
| --- | --- | --- | --- |
| `year` | no | most recent year with data | Four-digit year. A blank `?year=` is treated as omitted. |
| `limit` | no | `10` | Clamped to 1–100. Unparseable values fall back to 10. |

**Response** — `200`, a JSON array, ordered by `total_co2` descending:

```json
[
  {
    "facility_id": 8,
    "facility_name": "James H Miller Jr",
    "state_code": "AL",
    "total_co2": 3950000.0,
    "year": 2023
  },
  {
    "facility_id": 3,
    "facility_name": "Barry",
    "state_code": "AL",
    "total_co2": 900000.0,
    "year": 2023
  }
]
```

| Field | Type | Notes |
| --- | --- | --- |
| `facility_id` | integer | EPA facility ID. **Use this as your key, not the name.** |
| `facility_name` | string | Plant name. **Not unique** — see the warning below. |
| `state_code` | string | Two-letter code. May be `""` if the source file omitted it. |
| `total_co2` | float | Short tons, summed across every unit at the facility, rounded to 2 dp. |
| `year` | integer | The year these totals cover. Same on every row; read `data[0].year` for a chart title. |

> **Plant names repeat across states.** There are multiple facilities named
> "Barry". Two rows can carry the same `facility_name` and be different plants.
> Key on `facility_id`, and if you show a label, consider
> `` `${facility_name} (${state_code})` ``.

**Totals are never blended across years.** With no `?year=`, the endpoint
resolves the latest year in the database and reports only that year. This is
deliberate: summing all years would rank a facility with three years of data
above one with a single year.

**Edge cases**

| Case | Status | Body |
| --- | --- | --- |
| No data ingested at all | `200` | `[]` |
| Valid year with no records (`?year=1999`) | `200` | `[]` |
| Unparseable year (`?year=abc`) | `400` | `{"error": "Invalid year 'abc'.", "detail": "Expected a four-digit year, for example 2023."}` |
| Database failure | `500` | `{"error": "Database query failed", "detail": "..."}` |

Handle `[]` — a fresh database returns it, and so does any year with no data.

### `GET /api/emissions/years`

Every reporting year that has records, newest first. Use it to populate a year
selector so the dropdown only ever offers years that exist.

**Response** — `200`, a flat array of integers:

```json
[2023, 2022, 2021]
```

Returns `[]` when nothing has been ingested. Errors use the same
`{"error": ..., "detail": ...}` shape as above.

---

## 4. JavaScript integration

Fetching the chart data and splitting it into Plotly's parallel arrays:

```js
fetch("/api/emissions/top-facilities")
  .then((response) => {
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
  })
  .then((data) => {
    if (!data.length) {
      document.getElementById("chart-status").textContent =
        "No emissions data ingested yet.";
      return;
    }

    const names = data.map((row) => `${row.facility_name} (${row.state_code})`);
    const totals = data.map((row) => row.total_co2);

    Plotly.newPlot(
      "co2-chart",
      [{ type: "bar", x: names, y: totals, name: "CO₂ (short tons)" }],
      {
        title: `Top facilities by CO₂ — ${data[0].year}`,
        yaxis: { title: "CO₂ (short tons)" },
        margin: { b: 120 },
      },
    );
  })
  .catch((error) => console.error("Chart load failed:", error));
```

Adding the year filter, driven by `/api/emissions/years`:

```js
// Populate the <select>, then refetch with ?year= on change.
fetch("/api/emissions/years")
  .then((r) => r.json())
  .then((years) => {
    const select = document.getElementById("year-select");
    select.innerHTML = years
      .map((y) => `<option value="${y}">${y}</option>`)
      .join("");
    select.addEventListener("change", () => loadChart(select.value));
  });

function loadChart(year) {
  const url = year
    ? `/api/emissions/top-facilities?year=${encodeURIComponent(year)}`
    : "/api/emissions/top-facilities";
  fetch(url).then((r) => r.json()).then(drawChart);
}
```

A working `fetch` + `console.log` starter is already in
`app/templates/explorer.html` under `{% block scripts %}` — replace the
`console.log` with your `Plotly.newPlot` call.

---

## 5. Git workflow

Backend routes, API response shapes, and `base.html` all change on `main`.
Pull before you start work and before you test:

```bash
git pull origin main
```

Testing against a stale `base.html` is the most likely way to waste an
afternoon: a block gets renamed or a navbar entry is added, your page renders
with a missing layout, and the bug looks like it's in your code.

Running the app locally:

```bash
source .venv/bin/activate     # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
flask db upgrade              # after any pull that touched migrations/
flask run
```

Then open <http://127.0.0.1:5000/>. `FLASK_DEBUG=1` in `.env` reloads the
server on save — but **template and static file changes need a hard refresh**
(Ctrl/Cmd+Shift+R) because the browser caches them.

If the database is empty, the charts get `[]`. Load data first at `/upload`
with a CAMPD CSV or XLSX export.

---

## Quick reference

| What | Where |
| --- | --- |
| HTML | `app/templates/` |
| CSS / JS / images | `app/static/css`, `app/static/js`, `app/static/img` |
| Static URLs | `{{ url_for('static', filename='js/explorer.js') }}` |
| Layout to extend | `base.html` |
| Your markup | `{% block content %}` |
| Your scripts | `{% block scripts %}` |
| Chart data | `GET /api/emissions/top-facilities?year=YYYY&limit=N` |
| Year list | `GET /api/emissions/years` |
| Plotly | Not loaded by `base.html` — add the CDN tag yourself |
