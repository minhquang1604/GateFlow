// Gateflow Management Console — vanilla JS, no framework and no build step.
// The shell is composed server-side in ui/mount.py; every page calls its own
// init function; all data comes from the JSON API.

const API = "/api";

/* ------------------------------------------------------------------ */
/* Console shell — side-nav toggle and theme                           */
/* ------------------------------------------------------------------ */

const NAV_BREAKPOINT = 900; // keep in sync with the media query in app.css
const THEME_KEY = "gateflow-theme";

// Called from the shell in ui/mount.py, on every page.
function initShell() {
  const body = document.body;
  const toggle = document.getElementById("nav-toggle");
  const scrim = document.getElementById("nav-scrim");

  // One button, two behaviours: on a wide viewport it folds the menu
  // away beside the content; on a narrow one it opens a drawer over it.
  const isNarrow = () => window.innerWidth <= NAV_BREAKPOINT;

  function setExpanded() {
    const open = isNarrow()
      ? body.classList.contains("nav-open")
      : !body.classList.contains("nav-collapsed");
    toggle.setAttribute("aria-expanded", String(open));
    scrim.hidden = !body.classList.contains("nav-open");
  }

  function closeDrawer() {
    body.classList.remove("nav-open");
    setExpanded();
  }

  toggle.addEventListener("click", () => {
    body.classList.toggle(isNarrow() ? "nav-open" : "nav-collapsed");
    setExpanded();
  });
  scrim.addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeDrawer();
  });
  // Resizing past the breakpoint leaves the drawer state stale otherwise.
  window.addEventListener("resize", () => {
    if (!isNarrow()) closeDrawer();
    else setExpanded();
  });
  setExpanded();

  const themeBtn = document.getElementById("theme-toggle");
  themeBtn.addEventListener("click", () => {
    const root = document.documentElement;
    const dark = root.dataset.theme
      ? root.dataset.theme === "dark"
      : window.matchMedia("(prefers-color-scheme: dark)").matches;
    root.dataset.theme = dark ? "light" : "dark";
    try { localStorage.setItem(THEME_KEY, root.dataset.theme); } catch (e) { /* private mode */ }
  });

  for (const b of document.querySelectorAll('[data-action="reload"]')) {
    b.addEventListener("click", () => location.reload());
  }
}

async function api(path, opts = {}) {
  const r = await fetch(API + path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!r.ok) {
    const text = await r.text().catch(() => "");
    throw new Error(`${r.status} ${r.statusText}: ${text}`);
  }
  if (r.status === 204) return null;
  return r.json();
}

function el(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k === "html") e.innerHTML = v;
    else if (k.startsWith("on") && typeof v === "function") {
      e.addEventListener(k.substring(2).toLowerCase(), v);
    } else if (v !== null && v !== undefined && v !== false) {
      e.setAttribute(k, v);
    }
  }
  for (const c of children) {
    if (c == null || c === false) continue;
    if (Array.isArray(c)) { for (const x of c) if (x != null) e.appendChild(x); continue; }
    if (typeof c === "string" || typeof c === "number") {
      e.appendChild(document.createTextNode(String(c)));
    } else e.appendChild(c);
  }
  return e;
}

function svgEl(tag, attrs = {}, ...children) {
  const e = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v !== null && v !== undefined) e.setAttribute(k, v);
  }
  for (const c of children) {
    if (c == null) continue;
    if (typeof c === "string" || typeof c === "number") {
      e.appendChild(document.createTextNode(String(c)));
    } else e.appendChild(c);
  }
  return e;
}

function statusKind(status) {
  const s = String(status || "").toLowerCase();
  if (["success", "ready", "production", "passed", "approved_ok"].includes(s)) return "success";
  if (["failed", "rejected", "blocked", "upstream_failed"].includes(s)) return "failed";
  if (["running", "queued"].includes(s)) return "running";
  if (["pending", "training", "scheduled", "candidate"].includes(s)) return "pending";
  if (["cancelled", "archived", "skipped", "removed"].includes(s)) return "cancelled";
  if (s === "approved") return "approved";
  return "";
}

function statusBadge(status) {
  if (status == null || status === "") return el("span", { class: "faint" }, "—");
  return el("span", { class: `badge ${statusKind(status)}` }, String(status));
}

// The API serves timestamps straight off the ORM. On SQLite those come
// back without a timezone offset even though the framework writes UTC,
// and `new Date("...")` reads an offset-less string as *local* time —
// which silently shifts every "x ago" by the viewer's UTC offset. Pin
// them to UTC unless the string already says otherwise.
function parseTs(s) {
  if (!s) return null;
  // MLflow reports times as epoch milliseconds, the framework's own rows as
  // ISO strings. Both reach these formatters, so accept a number directly
  // rather than running it through the string path, where appending "Z"
  // would turn it into an Invalid Date and silently render "—".
  if (typeof s === "number") {
    const d = new Date(s);
    return isNaN(d.getTime()) ? null : d;
  }
  const hasZone = /[zZ]$|[+-]\d{2}:?\d{2}$/.test(s);
  const d = new Date(hasZone ? s : s + "Z");
  return isNaN(d.getTime()) ? null : d;
}

const fmt = {
  pct(x) { return x == null ? "—" : (x * 100).toFixed(1) + "%"; },
  num(x) { return x == null ? "—" : Number(x).toLocaleString(); },
  metric(x) {
    if (x == null) return "—";
    if (typeof x !== "number") return String(x);
    if (Number.isInteger(x)) return x.toLocaleString();
    return Math.abs(x) >= 1000 ? x.toFixed(1) : x.toFixed(4);
  },
  time(s) {
    const d = parseTs(s);
    return d ? d.toLocaleString() : "—";
  },
  ago(s) {
    const t = parseTs(s);
    if (!t) return "—";
    const d = (Date.now() - t.getTime()) / 1000;
    if (!isFinite(d)) return "—";
    if (d < 0) return "just now";
    if (d < 60) return `${Math.round(d)}s ago`;
    if (d < 3600) return `${Math.round(d / 60)}m ago`;
    if (d < 86400) return `${Math.round(d / 3600)}h ago`;
    return `${Math.round(d / 86400)}d ago`;
  },
  dur(sec) {
    if (sec == null) return "—";
    if (sec < 1) return `${(sec * 1000).toFixed(0)}ms`;
    if (sec < 60) return `${sec.toFixed(1)}s`;
    const m = Math.floor(sec / 60), s = Math.round(sec % 60);
    if (m < 60) return `${m}m ${s}s`;
    return `${Math.floor(m / 60)}h ${m % 60}m`;
  },
  bytes(b) {
    if (b == null) return "—";
    const u = ["B", "KB", "MB", "GB"];
    let i = 0, n = Number(b);
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return `${n.toFixed(i ? 1 : 0)} ${u[i]}`;
  },
  hash(h, n = 12) { return !h ? "—" : h.substring(0, n) + (h.length > n ? "…" : ""); },
};

function banner(msg, kind = "") {
  return el("div", { class: `banner ${kind}` }, msg);
}

function emptyRow(colspan, msg) {
  return el("tr", {}, el("td", { colspan: String(colspan), class: "empty" }, msg));
}

function setError(container, e) {
  container.replaceChildren(banner(`Could not load: ${e.message}`, "err"));
}

// `Node.replaceChildren` stringifies whatever it is handed, so a
// conditional child written as `cond ? node : null` renders the literal
// text "null". The el() helper drops nullish children; this makes the
// container-level call behave the same way.
function mount(container, ...children) {
  container.replaceChildren(...children.flat().filter((c) => c != null && c !== false));
}

/* ------------------------------------------------------------------ */
/* Charts — hand-rolled inline SVG. No chart library, no build step.    */
/* ------------------------------------------------------------------ */

// A line chart of {step, value} points. Sized in a viewBox so it scales
// with its container rather than needing a resize listener.
function lineChart(title, points, opts = {}) {
  const W = 320, H = 140, P = { t: 8, r: 8, b: 20, l: 38 };
  const wrap = el("div", { class: "chart" }, el("div", { class: "chart-title" }, title));
  if (!points || points.length === 0) {
    wrap.appendChild(el("div", { class: "empty" }, "No history"));
    return wrap;
  }

  const xs = points.map((p) => p.step);
  const ys = points.map((p) => p.value);
  const xMin = Math.min(...xs), xMax = Math.max(...xs);
  let yMin = Math.min(...ys), yMax = Math.max(...ys);
  if (yMin === yMax) { yMin -= Math.abs(yMin) * 0.1 || 0.5; yMax += Math.abs(yMax) * 0.1 || 0.5; }
  const pad = (yMax - yMin) * 0.08;
  yMin -= pad; yMax += pad;

  const sx = (x) => P.l + ((x - xMin) / (xMax - xMin || 1)) * (W - P.l - P.r);
  const sy = (y) => H - P.b - ((y - yMin) / (yMax - yMin || 1)) * (H - P.t - P.b);

  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": title });

  for (let i = 0; i <= 3; i++) {
    const v = yMin + ((yMax - yMin) * i) / 3;
    const y = sy(v);
    svg.appendChild(svgEl("line", { class: "gridline", x1: P.l, y1: y, x2: W - P.r, y2: y }));
    svg.appendChild(svgEl("text", { class: "tick-label", x: P.l - 5, y: y + 3, "text-anchor": "end" }, v.toFixed(3)));
  }
  svg.appendChild(svgEl("line", { class: "axis", x1: P.l, y1: H - P.b, x2: W - P.r, y2: H - P.b }));

  const d = points.map((p, i) => `${i ? "L" : "M"}${sx(p.step).toFixed(2)},${sy(p.value).toFixed(2)}`).join(" ");
  svg.appendChild(svgEl("path", { class: "line", d, stroke: opts.color || null }));
  if (points.length <= 40) {
    for (const p of points) {
      svg.appendChild(svgEl("circle", { class: "point", cx: sx(p.step), cy: sy(p.value), r: 2.5, fill: opts.color || null }));
    }
  }
  svg.appendChild(svgEl("text", { class: "tick-label", x: P.l, y: H - 6 }, String(xMin)));
  svg.appendChild(svgEl("text", { class: "tick-label", x: W - P.r, y: H - 6, "text-anchor": "end" }, String(xMax)));

  wrap.appendChild(svg);
  return wrap;
}

// Horizontal bar comparison, used to compare one metric across runs or
// model versions. Bars share a scale so lengths are directly comparable.
function barChart(title, entries) {
  const wrap = el("div", { class: "chart" }, el("div", { class: "chart-title" }, title));
  if (!entries.length) {
    wrap.appendChild(el("div", { class: "empty" }, "No data"));
    return wrap;
  }
  const max = Math.max(...entries.map((e) => Math.abs(e.value)), 1e-9);
  const rows = el("div", {});
  for (const e of entries) {
    rows.appendChild(
      el("div", { style: "display:flex;align-items:center;gap:10px;margin:6px 0" },
        el("span", { class: "mono", style: "min-width:96px" }, e.label),
        el("div", { class: "bar-track", style: "flex:1" },
          el("div", { class: `bar-fill ${e.kind || ""}`, style: `width:${(Math.abs(e.value) / max) * 100}%` })),
        el("span", { class: "mono", style: "min-width:64px;text-align:right" }, fmt.metric(e.value)))
    );
  }
  wrap.appendChild(rows);
  return wrap;
}

/* ------------------------------------------------------------------ */
/* Sortable table helper                                               */
/* ------------------------------------------------------------------ */

function makeSortable(table, rows, columns, render) {
  let sortKey = null, sortDir = -1;
  const thead = table.querySelector("thead tr");
  const tbody = table.querySelector("tbody");

  function draw() {
    const data = rows.slice();
    if (sortKey) {
      data.sort((a, b) => {
        const x = sortKey(a), y = sortKey(b);
        if (x == null && y == null) return 0;
        if (x == null) return 1;
        if (y == null) return -1;
        return (x > y ? 1 : x < y ? -1 : 0) * sortDir;
      });
    }
    tbody.replaceChildren(...data.map(render));
    if (!data.length) tbody.appendChild(emptyRow(columns.length, "Nothing to show."));
  }

  thead.replaceChildren(
    ...columns.map((c) => {
      const th = el("th", { class: c.sort ? "sortable" : "" }, c.label);
      if (c.sort) {
        th.addEventListener("click", () => {
          if (sortKey === c.sort) sortDir = -sortDir;
          else { sortKey = c.sort; sortDir = -1; }
          for (const other of thead.querySelectorAll(".arrow")) other.remove();
          th.appendChild(el("span", { class: "arrow" }, sortDir < 0 ? "▾" : "▴"));
          draw();
        });
      }
      return th;
    })
  );
  draw();
  return draw;
}

/* ------------------------------------------------------------------ */
/* Dashboard                                                           */
/* ------------------------------------------------------------------ */

async function initDashboard() {
  const grid = document.getElementById("kpi-grid");
  try {
    const d = await api("/dashboard");
    const items = [
      { label: "Datasets", value: d.datasets },
      { label: "Dataset versions", value: d.dataset_versions },
      { label: "Total runs", value: d.total_runs },
      { label: "Active runs", value: d.active_runs, kind: d.active_runs > 0 ? "warn" : "" },
      { label: "Successful", value: d.success_runs, kind: "ok" },
      { label: "Failed", value: d.failed_runs, kind: d.failed_runs > 0 ? "err" : "" },
      { label: "Models", value: d.models },
      { label: "In production", value: d.production_models, kind: "ok" },
      {
        label: "Success rate",
        value: fmt.pct(d.success_rate),
        kind: d.success_rate >= 0.8 ? "ok" : d.success_rate >= 0.5 ? "warn" : "err",
      },
    ];
    grid.replaceChildren(
      ...items.map((i) =>
        el("div", { class: `kpi ${i.kind || ""}` },
          el("div", { class: "label" }, i.label),
          el("div", { class: "value" }, i.value == null ? "—" : String(i.value))))
    );
  } catch (e) {
    setError(grid, e);
    return;
  }

  // Recent activity — the Airflow-style run strip plus the latest rows.
  const recent = document.getElementById("recent-runs");
  if (!recent) return;
  try {
    const runs = await api("/training-runs?limit=40");
    const strip = el("div", { class: "run-strip" },
      ...runs.slice().reverse().map((r) =>
        el("div", {
          class: `tick ${statusKind(r.status)}`,
          title: `Run ${r.id} — ${r.status}${r.duration_seconds != null ? " — " + fmt.dur(r.duration_seconds) : ""}`,
          style: `height:${Math.max(8, Math.min(34, (r.duration_seconds || 1) / 8 + 8))}px`,
        })));

    const table = el("table", {}, el("thead", {}, el("tr", {})), el("tbody", {}));
    recent.replaceChildren(
      el("div", { class: "card", style: "margin-bottom:16px" },
        el("div", { class: "chart-title", style: "margin-bottom:8px" },
          "Recent runs — bar height is duration, colour is status"),
        strip),
      el("div", { class: "table-wrap" }, table));

    makeSortable(table, runs.slice(0, 10),
      [{ label: "Run" }, { label: "Status" }, { label: "Pipeline" }, { label: "Duration" }, { label: "Started" }],
      (r) => el("tr", {},
        el("td", {}, el("a", { href: `/runs/${r.id}` }, `#${r.id}`)),
        el("td", {}, statusBadge(r.status)),
        el("td", { class: "mono truncate", title: r.pipeline_id || "" }, r.pipeline_id || "—"),
        el("td", { class: "num" }, fmt.dur(r.duration_seconds)),
        el("td", { class: "muted nowrap" }, fmt.ago(r.started_at || r.created_at))));
  } catch (e) {
    setError(recent, e);
  }
}

/* ------------------------------------------------------------------ */
/* Datasets                                                            */
/* ------------------------------------------------------------------ */

async function initDatasets() {
  const table = document.querySelector("table");
  try {
    const rows = await api("/datasets");
    makeSortable(table, rows,
      [
        { label: "Name", sort: (d) => d.name },
        { label: "Description" },
        { label: "Versions", sort: (d) => d.version_count },
        { label: "Latest rows", sort: (d) => d.latest_version?.row_count },
        { label: "Schema" },
      ],
      (ds) => el("tr", {},
        el("td", {}, el("a", { href: `/datasets/${ds.id}` }, ds.name)),
        el("td", { class: "muted" }, ds.description || "—"),
        el("td", { class: "num" }, String(ds.version_count)),
        el("td", { class: "num" }, fmt.num(ds.latest_version?.row_count)),
        el("td", { class: "mono faint" }, fmt.hash(ds.latest_version?.schema_hash, 10))));
  } catch (e) {
    setError(table.parentElement, e);
  }
}

async function initDatasetDetail(id) {
  const head = document.getElementById("ds-head");
  const body = document.getElementById("ds-body");
  try {
    const [ds, versions] = await Promise.all([
      api(`/datasets/${id}`),
      api(`/datasets/${id}/versions`),
    ]);

    head.replaceChildren(
      el("div", { class: "breadcrumb" }, el("a", { href: "/datasets" }, "Datasets"), " / ", ds.name),
      el("h2", {}, ds.name),
      el("p", { class: "subtitle" }, ds.description || "No description"));

    const sections = [];

    for (const v of versions.slice().reverse()) {
      const meta = v.metadata || {};
      let readiness = null;
      try { readiness = await api(`/readiness/${v.id}`); } catch { /* optional */ }

      const facts = el("dl", { class: "kv" },
        el("dt", {}, "Rows"), el("dd", {}, fmt.num(v.row_count)),
        el("dt", {}, "Storage URI"), el("dd", {}, v.storage_uri),
        el("dt", {}, "Content SHA-256"), el("dd", {}, meta.content_sha256 || "not recorded"),
        el("dt", {}, "Schema hash"), el("dd", {}, v.schema_hash),
        el("dt", {}, "Version checksum"), el("dd", {}, v.checksum),
        el("dt", {}, "Size"), el("dd", {}, fmt.bytes(meta.size_bytes)),
        el("dt", {}, "Immutable"), el("dd", {}, v.is_immutable ? "yes" : "no"),
        el("dt", {}, "Created"), el("dd", {}, fmt.time(v.created_at)));

      const classBalance = meta.n_fraud != null
        ? el("div", { class: "card" },
            el("div", { class: "chart-title" }, "Class balance"),
            el("div", { class: "metric-grid" },
              el("div", { class: "metric" },
                el("div", { class: "name" }, "positive"), el("div", { class: "val" }, fmt.num(meta.n_fraud))),
              el("div", { class: "metric" },
                el("div", { class: "name" }, "ratio"), el("div", { class: "val" }, fmt.pct(meta.fraud_ratio))),
              el("div", { class: "metric" },
                el("div", { class: "name" }, "missing"), el("div", { class: "val" }, fmt.num(meta.missing_values)))))
        : null;

      const schemaRows = (meta.columns || []).map((c) =>
        el("tr", {},
          el("td", { class: "mono" }, c.name),
          el("td", { class: "mono muted" }, c.dtype)));

      const readinessPanel = el("div", { class: "card" },
        el("div", { class: "chart-title" }, "Readiness"),
        readiness
          ? el("div", {},
              el("div", { style: "margin-bottom:8px" }, statusBadge(readiness.status)),
              el("div", { class: "task-grid" },
                ...Object.entries(readiness.checks || {}).map(([name, outcome]) =>
                  el("div", { class: `task-cell ${statusKind(outcome)}` },
                    el("span", { class: "dot" }), name,
                    el("span", { class: "state" }, outcome)))),
              (readiness.reasons || []).length
                ? el("ul", { class: "muted", style: "margin:10px 0 0;padding-left:18px" },
                    ...readiness.reasons.map((r) => el("li", {}, r)))
                : null)
          : el("div", { class: "muted" }, "Not evaluated yet."));

      sections.push(
        el("section", {},
          el("div", { class: "section-head" },
            el("h3", {}, `Version ${v.version_number}`),
            el("span", { class: "faint" }, fmt.ago(v.created_at))),
          el("div", { class: "grid-2" },
            el("div", { class: "card" }, facts),
            el("div", {}, readinessPanel, classBalance ? el("div", { style: "height:16px" }) : null, classBalance)),
          schemaRows.length
            ? el("div", {},
                el("h3", {}, `Schema — ${schemaRows.length} columns`),
                el("div", { class: "table-wrap" },
                  el("table", {},
                    el("thead", {}, el("tr", {}, el("th", {}, "Column"), el("th", {}, "Dtype"))),
                    el("tbody", {}, ...schemaRows))))
            : null));
    }

    mount(body, ...(sections.length ? sections : [banner("No versions registered yet.")]));
  } catch (e) {
    setError(body, e);
  }
}

/* ------------------------------------------------------------------ */
/* Runs — Airflow-flavoured list with MLflow-style comparison          */
/* ------------------------------------------------------------------ */

async function initRuns() {
  const table = document.querySelector("table");
  const statusFilter = document.getElementById("status-filter");
  const search = document.getElementById("search");
  const compareBtn = document.getElementById("compare-btn");
  const selected = new Set();

  function updateCompare() {
    compareBtn.disabled = selected.size < 2;
    compareBtn.textContent = selected.size
      ? `Compare ${selected.size} run${selected.size > 1 ? "s" : ""}`
      : "Compare runs";
  }

  compareBtn.addEventListener("click", () => {
    location.href = `/runs/compare?ids=${[...selected].join(",")}`;
  });

  async function load() {
    let all;
    try {
      all = await api("/training-runs?limit=500");
    } catch (e) {
      setError(table.parentElement, e);
      return;
    }
    const q = (search.value || "").toLowerCase();
    const st = statusFilter.value;
    const rows = all.filter((r) =>
      (!st || r.status === st) &&
      (!q || `${r.id} ${r.pipeline_id || ""} ${r.orchestrator || ""}`.toLowerCase().includes(q)));

    const maxDur = Math.max(...rows.map((r) => r.duration_seconds || 0), 1);

    makeSortable(table, rows,
      [
        { label: "" },
        { label: "Run", sort: (r) => r.id },
        { label: "Status", sort: (r) => r.status },
        { label: "Pipeline", sort: (r) => r.pipeline_id },
        { label: "Orchestrator" },
        { label: "Trigger", sort: (r) => r.trigger_type },
        { label: "Duration", sort: (r) => r.duration_seconds },
        { label: "Key metric", sort: (r) => bestMetric(r)?.value },
        { label: "Started", sort: (r) => r.started_at },
      ],
      (r) => {
        const best = bestMetric(r);
        const cb = el("input", { type: "checkbox" });
        cb.checked = selected.has(r.id);
        cb.addEventListener("change", () => {
          if (cb.checked) selected.add(r.id); else selected.delete(r.id);
          updateCompare();
        });
        return el("tr", {},
          el("td", { class: "checkbox-cell" }, cb),
          el("td", {}, el("a", { href: `/runs/${r.id}` }, `#${r.id}`)),
          el("td", {}, statusBadge(r.status)),
          el("td", { class: "mono truncate", title: r.pipeline_id || "" }, r.pipeline_id || "—"),
          el("td", { class: "muted" }, r.orchestrator || "—"),
          el("td", { class: "muted" }, r.trigger_type || "—"),
          el("td", {},
            el("div", { style: "display:flex;align-items:center;gap:8px" },
              el("span", { class: "mono nowrap", style: "min-width:56px" }, fmt.dur(r.duration_seconds)),
              el("div", { class: "bar-track", style: "width:70px" },
                el("div", {
                  class: `bar-fill ${statusKind(r.status)}`,
                  style: `width:${((r.duration_seconds || 0) / maxDur) * 100}%`,
                })))),
          el("td", { class: "num" }, best ? `${best.name} ${fmt.metric(best.value)}` : "—"),
          el("td", { class: "muted nowrap" }, fmt.ago(r.started_at || r.created_at)));
      });
    updateCompare();
  }

  statusFilter.addEventListener("change", load);
  search.addEventListener("input", load);
  document.getElementById("refresh").addEventListener("click", load);
  await load();
}

// Pick the metric worth showing in a list. Ordered by how much it says
// about an imbalanced classifier, which is what this framework trains.
const METRIC_PRIORITY = ["average_precision", "f1", "roc_auc", "recall", "precision", "accuracy"];

function bestMetric(run) {
  const m = run.metrics || {};
  for (const name of METRIC_PRIORITY) {
    if (typeof m[name] === "number") return { name, value: m[name] };
  }
  const first = Object.entries(m).find(([, v]) => typeof v === "number");
  return first ? { name: first[0], value: first[1] } : null;
}

// Fetches one task attempt's log and renders it as plain text below the
// task grid. Not an api()-wrapped call: the endpoint answers with the log
// body directly (mirroring how mlflow_views.get_run_artifact serves raw
// bytes), including a 200 whose *body* is Airflow's own error message
// when it could not reach where the log actually lives — that text is
// shown as-is, since it is the accurate answer, not a failure to hide.
function showTaskLog(host, runId, taskId, tryNumber) {
  host.replaceChildren(
    el("div", { class: "section-head", style: "margin-top:12px" },
      el("div", { class: "chart-title" }, `Log — ${taskId} (attempt ${tryNumber})`)),
    el("pre", { class: "log" }, "Loading…"));
  const pre = host.querySelector("pre");
  fetch(`${API}/training-runs/${runId}/tasks/${encodeURIComponent(taskId)}/log?try_number=${tryNumber}`)
    .then((r) => r.text().then((text) => ({ ok: r.ok, status: r.status, text })))
    .then(({ ok, status, text }) => {
      pre.textContent = ok ? (text || "(empty log)") : `${status}: ${text || "could not load log"}`;
    })
    .catch(() => { pre.textContent = "Could not load log."; });
}

async function initRunDetail(id) {
  const head = document.getElementById("run-head");
  const body = document.getElementById("run-body");
  let run;
  try {
    run = await api(`/training-runs/${id}`);
  } catch (e) {
    setError(head, e);
    return;
  }

  head.replaceChildren(
    el("div", { class: "breadcrumb" }, el("a", { href: "/runs" }, "Runs"), " / ", `#${run.id}`),
    el("h2", {}, `Training run #${run.id} `, statusBadge(run.status)),
    el("p", { class: "subtitle mono" }, run.pipeline_id || "no pipeline"));

  const sections = [];

  if (run.error_message) {
    sections.push(el("div", {},
      el("h3", {}, "Failure"),
      el("pre", { class: "log" }, run.error_message)));
  }

  sections.push(el("div", { class: "grid-2" },
    el("div", { class: "card" },
      el("div", { class: "chart-title" }, "Run"),
      el("dl", { class: "kv" },
        el("dt", {}, "Status"), el("dd", {}, run.status),
        el("dt", {}, "Trigger"), el("dd", {}, run.trigger_type || "—"),
        el("dt", {}, "Orchestrator"), el("dd", {}, run.orchestrator || "—"),
        el("dt", {}, "Execution id"), el("dd", {}, run.execution_id || "—"),
        el("dt", {}, "Dataset version"), el("dd", {},
          run.dataset_version_id
            ? el("a", { href: `/lineage?kind=dataset-version&id=${run.dataset_version_id}` }, `#${run.dataset_version_id}`)
            : "—"),
        el("dt", {}, "MLflow run"), el("dd", {}, run.mlflow_run_id || "—"),
        el("dt", {}, "Started"), el("dd", {}, fmt.time(run.started_at)),
        el("dt", {}, "Completed"), el("dd", {}, fmt.time(run.completed_at)),
        el("dt", {}, "Duration"), el("dd", {}, fmt.dur(run.duration_seconds)))),
    el("div", { class: "card" },
      el("div", { class: "chart-title" }, "Parameters"),
      Object.keys(run.parameters || {}).length
        ? el("dl", { class: "kv" },
            ...Object.entries(run.parameters).flatMap(([k, v]) => [
              el("dt", {}, k), el("dd", {}, String(v))]))
        : el("div", { class: "muted" }, "No parameters recorded."))));

  const metrics = run.metrics || {};
  if (Object.keys(metrics).length) {
    const best = bestMetric(run);
    sections.push(el("div", {},
      el("h3", {}, "Metrics"),
      el("div", { class: "metric-grid" },
        ...Object.entries(metrics).map(([k, v]) =>
          el("div", { class: `metric ${best && best.name === k ? "best" : ""}` },
            el("div", { class: "name" }, k),
            el("div", { class: "val" }, fmt.metric(v)))))));
  }

  // Airflow: the DAG run's own state/dates/conf, plus a full task grid —
  // only rendered when the run actually ran there. Each task cell opens
  // its log on click; run.execution_id is already shown in the Run card
  // above, so this panel does not repeat it.
  const tasksPanel = el("div", {});
  sections.push(tasksPanel);
  api(`/training-runs/${id}/tasks`).then((p) => {
    if (!p.available) {
      if (run.orchestrator === "AirflowOrchestrator") {
        tasksPanel.replaceChildren(el("h3", {}, "Airflow"), banner(p.reason, "warn"));
      }
      return;
    }
    const dagRun = p.data.dag_run || {};
    const tasks = p.data.tasks || [];
    const logHost = el("div", {});

    const cells = tasks.map((t) => {
      const isRetry = t.try_number != null && t.try_number > 1;
      const cell = el("div", {
        class: `task-cell ${statusKind(t.state)}`,
        role: "button",
        tabindex: "0",
        title: "View log",
      },
        el("span", { class: "dot" }),
        el("div", {},
          el("div", {}, t.task_id,
            isRetry ? el("span", { class: "retry-badge" },
              `retry ${t.try_number}${t.max_tries ? `/${t.max_tries + 1}` : ""}`) : null),
          el("div", { class: "state" }, [
            t.state,
            t.duration != null ? fmt.dur(t.duration) : null,
            t.hostname || null,
          ].filter(Boolean).join(" · "))));
      const open = () => showTaskLog(logHost, id, t.task_id, t.try_number || 1);
      cell.addEventListener("click", open);
      cell.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } });
      return cell;
    });

    const confEntries = Object.entries(dagRun.conf || {});
    tasksPanel.replaceChildren(
      el("h3", {}, "Airflow"),
      el("div", { class: "card" },
        el("dl", { class: "kv", style: "margin-bottom:14px" },
          el("dt", {}, "DAG run"), el("dd", {}, statusBadge(dagRun.state)),
          el("dt", {}, "Started"), el("dd", {}, fmt.time(dagRun.started_at)),
          el("dt", {}, "Finished"), el("dd", {}, fmt.time(dagRun.finished_at))),
        confEntries.length
          ? el("details", { style: "margin-bottom:14px" },
              el("summary", { class: "faint" }, "Run conf"),
              el("pre", { class: "log", style: "margin-top:8px" },
                JSON.stringify(dagRun.conf, null, 2)))
          : null,
        tasks.length
          ? el("div", { class: "task-grid" }, ...cells)
          : el("div", { class: "muted" }, "No task instances — the DAG has not been scheduled."),
        logHost));
  }).catch(() => {});

  // MLflow: provenance, training curves, artifacts, model signature. Each
  // panel is appended in place so a slow or missing tracking server never
  // holds up the rest of the page.
  const mlPanel = el("div", {});
  const nestedPanel = el("div", {});
  const artifactPanel = el("div", {});
  const modelPanel = el("div", {});
  sections.push(mlPanel, nestedPanel, artifactPanel, modelPanel);

  api(`/training-runs/${id}/mlflow`).then((p) => {
    if (!p.available) {
      if (run.mlflow_run_id) {
        mlPanel.replaceChildren(el("h3", {}, "MLflow"), banner(p.reason, "warn"));
      }
      return;
    }
    const d = p.data;
    const hist = d.history || {};
    const charts = Object.entries(hist)
      .filter(([, series]) => series.length > 1)
      .map(([name, series]) => lineChart(name, series));

    // The experiment id comes from the run itself. It used to be hardcoded
    // to 0, which is wrong for every run outside the Default experiment.
    const expId = d.info?.experiment_id;
    const deepLink = `${d.tracking_uri}/#/experiments/${expId}/runs/${d.mlflow_run_id}`;

    // Resource usage is a different question from model quality, so it
    // gets its own row rather than being mixed into the training charts.
    const sysCharts = Object.entries(d.system_history || {})
      .filter(([, series]) => series.length > 1)
      .map(([name, series]) => lineChart(name.replace(/^system\//, ""), series));

    mount(mlPanel,
      el("div", { class: "section-head" },
        el("h3", {}, "MLflow"),
        el("a", { class: "faint", href: deepLink, target: "_blank", rel: "noopener" },
          "open in MLflow ↗")),
      provenanceCard(d),
      datasetInputsCard(d.dataset_inputs, run),
      charts.length
        ? el("div", { class: "grid-3", style: "margin-top:16px" }, ...charts)
        : el("div", { class: "card", style: "margin-top:16px" },
            el("div", { class: "muted" },
              "Metrics were logged once, so there is no series to plot. " +
              "Values are shown above."),
            el("dl", { class: "kv", style: "margin-top:10px" },
              ...Object.entries(d.metrics || {}).flatMap(([k, v]) => [
                el("dt", {}, k), el("dd", {}, fmt.metric(v))]))),
      sysCharts.length
        ? el("div", {},
            el("div", { class: "chart-title", style: "margin:20px 0 8px" },
              "System resources during the run"),
            el("div", { class: "grid-3" }, ...sysCharts))
        : null);
  }).catch(() => {});

  if (run.mlflow_run_id) {
    renderNestedRuns(nestedPanel, id);
    renderArtifacts(artifactPanel, id, "");
    renderModelInfo(modelPanel, id);
  }

  mount(body, ...sections);
}

/* ------------------------------------------------------------------ */
/* MLflow panels on the run detail page                                */
/* ------------------------------------------------------------------ */

// Tags MLflow sets itself. Shown as named fields rather than raw keys.
const PROVENANCE_FIELDS = [
  ["mlflow.runName", "Run name"],
  ["mlflow.user", "User"],
  ["mlflow.source.name", "Source"],
  ["mlflow.source.type", "Source type"],
  ["mlflow.source.git.commit", "Git commit"],
  ["mlflow.source.git.branch", "Git branch"],
];

function provenanceCard(d) {
  const tags = d.tags || {};
  const info = d.info || {};
  const rows = [];

  for (const [key, label] of PROVENANCE_FIELDS) {
    const v = tags[key];
    if (!v) continue;
    rows.push(el("dt", {}, label));
    rows.push(el("dd", { class: key.endsWith("commit") ? "mono" : "" }, v));
  }
  if (info.experiment_id != null) {
    rows.push(el("dt", {}, "Experiment"));
    rows.push(el("dd", {},
      el("a", { href: `/experiments/${encodeURIComponent(info.experiment_id)}` },
        `#${info.experiment_id}`)));
  }
  // MLflow's own view of the run, which can disagree with the framework's
  // row — a run the framework recorded as SUCCESS may be FAILED here if the
  // process died after the framework wrote its status.
  for (const [key, label] of [
    ["status", "MLflow status"],
    ["lifecycle_stage", "Lifecycle"],
    ["user_id", "Logged by"],
  ]) {
    if (info[key]) {
      rows.push(el("dt", {}, label));
      rows.push(el("dd", {}, String(info[key])));
    }
  }
  if (info.start_time) {
    rows.push(el("dt", {}, "MLflow start"));
    rows.push(el("dd", {}, fmt.time(info.start_time)));
  }
  if (info.end_time) {
    rows.push(el("dt", {}, "MLflow end"));
    rows.push(el("dd", {}, fmt.time(info.end_time)));
  }
  if (info.artifact_uri) {
    rows.push(el("dt", {}, "Artifact URI"));
    rows.push(el("dd", { class: "mono" }, info.artifact_uri));
  }

  // Tags the user set themselves, which is where domain meaning lives.
  const custom = Object.entries(tags).filter(([k]) => !k.startsWith("mlflow."));
  for (const [k, v] of custom) {
    rows.push(el("dt", {}, k));
    rows.push(el("dd", {}, v));
  }

  return el("div", { class: "card" },
    el("div", { class: "chart-title" }, "Provenance"),
    rows.length
      ? el("dl", { class: "kv" }, ...rows)
      : el("div", { class: "muted" }, "No tags recorded for this run."));
}

// What a run declared it trained on, per mlflow.log_input. The digest is
// content-derived, so it is the field worth holding against the framework's
// own dataset-version checksum.
function datasetInputsCard(inputs, run) {
  if (!inputs || !inputs.length) return null;
  return el("div", { class: "card", style: "margin-top:16px" },
    el("div", { class: "chart-title" }, "Dataset inputs (MLflow)"),
    el("div", { class: "table-wrap", style: "box-shadow:none;border:none" },
      el("table", {},
        el("thead", {}, el("tr", {},
          el("th", {}, "Name"), el("th", {}, "Digest"), el("th", {}, "Source"),
          el("th", {}, "Context"))),
        el("tbody", {}, ...inputs.map((i) =>
          el("tr", {},
            el("td", { class: "mono" }, i.name || "—"),
            el("td", { class: "mono" }, i.digest || "—"),
            el("td", { class: "mono truncate", title: i.source || "" },
              `${i.source_type || "?"}${i.source ? " · " + i.source : ""}`),
            el("td", { class: "muted" },
              (i.tags || {})["mlflow.data.context"] || "—")))))),
    run && run.dataset_version_id
      ? el("p", { class: "faint", style: "margin:10px 0 0;font-size:12.5px" },
          "Framework lineage records dataset version ",
          el("a", { href: `/lineage?kind=dataset-version&id=${run.dataset_version_id}` },
            `#${run.dataset_version_id}`),
          ". Compare the digest above against that version's checksum to " +
          "confirm the run trained on what the lineage claims.")
      : null);
}

function renderNestedRuns(host, runId) {
  api(`/training-runs/${runId}/nested`).then((p) => {
    if (!p.available) return;  // the MLflow panel above already said why
    const d = p.data;
    if (!d.parent && !(d.children || []).length) return;  // a standalone run

    const metricKeys = [...new Set((d.children || [])
      .flatMap((c) => Object.keys(c.metrics || {})))]
      .filter((k) => !k.startsWith("system/"))
      .sort();
    const best = {};
    for (const k of metricKeys) {
      best[k] = Math.max(...d.children.map((c) => (c.metrics || {})[k] ?? -Infinity));
    }
    const paramKeys = [...new Set((d.children || [])
      .flatMap((c) => Object.keys(c.params || {})))].sort();

    host.replaceChildren(
      el("div", { class: "section-head" },
        el("h3", {}, "Sweep"),
        el("span", { class: "faint" },
          d.is_child
            ? `this run is one trial of ${d.parent?.run_name || "a parent run"}`
            : "this run is the parent of the trials below")),
      el("div", { class: "table-wrap" },
        el("table", {},
          el("thead", {}, el("tr", {},
            el("th", {}, "Trial"),
            ...paramKeys.map((k) => el("th", {}, k)),
            ...metricKeys.map((k) => el("th", {}, k)),
            el("th", {}, "Started"))),
          el("tbody", {}, ...d.children.map((c) =>
            el("tr", { style: c.is_self ? "background:var(--accent-soft)" : null },
              el("td", {},
                el("span", { class: "mono", title: c.run_id },
                  c.run_name || c.run_id.slice(0, 8)),
                c.is_self ? el("span", { class: "faint" }, "  ← this run") : null),
              ...paramKeys.map((k) => el("td", { class: "mono" }, (c.params || {})[k] ?? "—")),
              ...metricKeys.map((k) => {
                const v = (c.metrics || {})[k];
                const isBest = typeof v === "number" && v === best[k] && d.children.length > 1;
                return el("td", { class: "num" },
                  isBest ? el("strong", { style: "color:var(--ok)" }, fmt.metric(v))
                         : fmt.metric(v));
              }),
              el("td", { class: "muted nowrap" }, fmt.ago(c.start_time))))))));
  }).catch(() => {});
}

const IMAGE_RE = /\.(png|jpe?g|gif|svg|webp)$/i;
const TEXT_RE = /\.(txt|json|ya?ml|csv|md|log|cfg|ini|requirements)$/i;

// Renders one directory of a run's artifacts, and recurses on click. Kept
// as an explicit re-render rather than a tree widget: the API is already
// per-directory, so this matches what the server can answer in one call.
function renderArtifacts(host, runId, path) {
  host.replaceChildren(el("h3", {}, "Artifacts"),
    el("div", { class: "card muted" }, "Loading…"));

  api(`/training-runs/${runId}/artifacts?path=${encodeURIComponent(path)}`).then((p) => {
    if (!p.available) {
      host.replaceChildren(el("h3", {}, "Artifacts"), banner(p.reason, "warn"));
      return;
    }
    const entries = p.data.entries || [];
    const crumbs = el("div", { class: "breadcrumb" },
      el("a", { href: "#", onclick: "return false" }, "artifacts"));
    crumbs.firstChild.addEventListener("click", () => renderArtifacts(host, runId, ""));
    let acc = "";
    for (const part of (path ? path.split("/") : [])) {
      acc = acc ? `${acc}/${part}` : part;
      const here = acc;
      crumbs.appendChild(document.createTextNode(" / "));
      const link = el("a", { href: "#" }, part);
      link.addEventListener("click", (e) => { e.preventDefault(); renderArtifacts(host, runId, here); });
      crumbs.appendChild(link);
    }

    const rawUrl = (p2) =>
      `${API}/training-runs/${runId}/artifacts/raw?path=${encodeURIComponent(p2)}`;

    const list = el("div", { class: "table-wrap" },
      el("table", {},
        el("thead", {}, el("tr", {},
          el("th", {}, "Name"), el("th", {}, "Size"), el("th", {}, ""))),
        el("tbody", {}, ...(entries.length ? entries.map((e) => {
          const nameCell = el("td", {});
          if (e.is_dir) {
            const a = el("a", { href: "#" }, `${e.name}/`);
            a.addEventListener("click", (ev) => {
              ev.preventDefault();
              renderArtifacts(host, runId, e.path);
            });
            nameCell.appendChild(a);
          } else {
            nameCell.appendChild(el("span", { class: "mono" }, e.name));
          }
          return el("tr", {},
            nameCell,
            el("td", { class: "num" }, e.is_dir ? "—" : fmt.bytes(e.file_size)),
            el("td", {}, e.is_dir ? "" :
              el("a", { href: rawUrl(e.path), target: "_blank", rel: "noopener" }, "open")));
        }) : [emptyRow(3, "No artifacts in this directory.")]))));

    // Inline previews for the things people actually came to look at:
    // the confusion matrix and the pinned dependency list.
    const previews = [];
    for (const e of entries) {
      if (e.is_dir) continue;
      if (IMAGE_RE.test(e.name)) {
        previews.push(el("div", { class: "card" },
          el("div", { class: "chart-title" }, e.name),
          el("img", {
            src: rawUrl(e.path), alt: e.name, loading: "lazy",
            style: "max-width:100%;height:auto;display:block;border-radius:4px",
          })));
      } else if (TEXT_RE.test(e.name) && (e.file_size || 0) <= 64 * 1024) {
        const pre = el("pre", { class: "log" }, "Loading…");
        fetch(rawUrl(e.path))
          .then((r) => r.text())
          .then((t) => { pre.textContent = t; })
          .catch(() => { pre.textContent = "Could not load."; });
        previews.push(el("div", { class: "card" },
          el("div", { class: "chart-title" }, e.name), pre));
      }
    }

    mount(host,
      el("h3", {}, "Artifacts"),
      crumbs,
      list,
      previews.length
        ? el("div", { class: "grid-2", style: "margin-top:16px" }, ...previews)
        : null);
  }).catch(() => {});
}

function renderModelInfo(host, runId) {
  api(`/training-runs/${runId}/model-info`).then((p) => {
    if (!p.available) {
      host.replaceChildren(el("h3", {}, "Model"), banner(p.reason, "warn"));
      return;
    }
    const d = p.data;
    if (!d.found) {
      host.replaceChildren(el("h3", {}, "Model"), banner(d.note || "No model logged."));
      return;
    }

    const sigTable = (title, specs) => {
      if (!Array.isArray(specs) || !specs.length) return null;
      return el("div", {},
        el("div", { class: "chart-title", style: "margin:12px 0 6px" }, title),
        el("div", { class: "table-wrap" },
          el("table", {},
            el("thead", {}, el("tr", {},
              el("th", {}, "Name"), el("th", {}, "Type"), el("th", {}, "Shape"))),
            el("tbody", {}, ...specs.map((s) => {
              const spec = s["tensor-spec"] || {};
              return el("tr", {},
                el("td", { class: "mono" }, s.name != null ? String(s.name) : "—"),
                el("td", { class: "mono" }, spec.dtype || s.type || "—"),
                el("td", { class: "mono" },
                  spec.shape ? `[${spec.shape.join(", ")}]` : "—"));
            })))));
    };

    const sig = d.signature || {};
    const env = [];
    for (const [flavor, detail] of Object.entries(d.flavor_detail || {})) {
      for (const [k, v] of Object.entries(detail || {})) {
        env.push(el("dt", {}, `${flavor}.${k}`));
        env.push(el("dd", { class: "mono" }, String(v)));
      }
    }

    host.replaceChildren(
      el("h3", {}, "Model"),
      el("div", { class: "grid-2" },
        el("div", { class: "card" },
          el("div", { class: "chart-title" }, "Flavors and environment"),
          el("dl", { class: "kv" },
            el("dt", {}, "Flavors"),
            el("dd", {}, (d.flavors || []).join(", ") || "—"),
            el("dt", {}, "MLflow version"),
            el("dd", {}, d.mlflow_version || "—"),
            el("dt", {}, "Logged at"),
            el("dd", {}, d.utc_time_created || "—"),
            el("dt", {}, "Layout"),
            el("dd", { class: "faint" },
              d.layout === "logged-model"
                ? "MLflow 3 logged model"
                : "run artifact (MLflow 2)"),
            ...env)),
        el("div", { class: "card" },
          el("div", { class: "chart-title" }, "Signature"),
          sigTable("Inputs", sig.inputs) || el("div", { class: "muted" }, "No input schema."),
          sigTable("Outputs", sig.outputs))));
  }).catch(() => {});
}

async function initRunsCompare() {
  const body = document.getElementById("compare-body");
  const ids = (new URLSearchParams(location.search).get("ids") || "")
    .split(",").map((s) => s.trim()).filter(Boolean);

  if (ids.length < 2) {
    body.replaceChildren(banner("Pick at least two runs on the Runs page to compare them."));
    return;
  }

  let runs;
  try {
    runs = await Promise.all(ids.map((i) => api(`/training-runs/${i}`)));
  } catch (e) {
    setError(body, e);
    return;
  }

  const paramKeys = [...new Set(runs.flatMap((r) => Object.keys(r.parameters || {})))].sort();
  const metricKeys = [...new Set(runs.flatMap((r) => Object.keys(r.metrics || {})))].sort();

  // Differing rows first — that is the whole reason to open this page.
  function differs(get) {
    const vals = runs.map((r) => JSON.stringify(get(r) ?? null));
    return new Set(vals).size > 1;
  }

  function compareTable(title, keys, get) {
    const changed = keys.filter((k) => differs((r) => get(r)[k]));
    const same = keys.filter((k) => !changed.includes(k));
    const rows = [...changed, ...same].map((k) => {
      const values = runs.map((r) => get(r)[k]);
      const numeric = values.filter((v) => typeof v === "number");
      const max = numeric.length ? Math.max(...numeric) : null;
      return el("tr", {},
        el("td", { class: "mono" }, k, changed.includes(k) ? el("span", { class: "faint" }, " ●") : null),
        ...values.map((v) =>
          el("td", { class: "num" },
            typeof v === "number" && v === max && numeric.length > 1
              ? el("strong", { style: "color:var(--ok)" }, fmt.metric(v))
              : fmt.metric(v))));
    });
    return el("div", {},
      el("h3", {}, title),
      el("div", { class: "table-wrap" },
        el("table", {},
          el("thead", {}, el("tr", {},
            el("th", {}, "Key"),
            ...runs.map((r) => el("th", {}, el("a", { href: `/runs/${r.id}` }, `#${r.id}`))))),
          el("tbody", {}, ...(rows.length ? rows : [emptyRow(runs.length + 1, "Nothing recorded.")])))));
  }

  const overview = el("div", { class: "table-wrap" },
    el("table", {},
      el("thead", {}, el("tr", {},
        el("th", {}, "Run"), el("th", {}, "Status"), el("th", {}, "Pipeline"),
        el("th", {}, "Orchestrator"), el("th", {}, "Duration"), el("th", {}, "Started"))),
      el("tbody", {}, ...runs.map((r) =>
        el("tr", {},
          el("td", {}, el("a", { href: `/runs/${r.id}` }, `#${r.id}`)),
          el("td", {}, statusBadge(r.status)),
          el("td", { class: "mono" }, r.pipeline_id || "—"),
          el("td", { class: "muted" }, r.orchestrator || "—"),
          el("td", { class: "num" }, fmt.dur(r.duration_seconds)),
          el("td", { class: "muted nowrap" }, fmt.time(r.started_at)))))));

  const headline = METRIC_PRIORITY.find((m) => runs.some((r) => typeof (r.metrics || {})[m] === "number"));
  const chart = headline
    ? barChart(headline, runs.map((r) => ({
        label: `#${r.id}`,
        value: (r.metrics || {})[headline] ?? 0,
        kind: statusKind(r.status),
      })))
    : null;

  mount(body,
    overview,
    chart ? el("div", { style: "margin-top:16px;max-width:520px" }, chart) : null,
    compareTable("Metrics — ● marks a value that differs", metricKeys, (r) => r.metrics || {}),
    compareTable("Parameters — ● marks a value that differs", paramKeys, (r) => r.parameters || {}));
}

/* ------------------------------------------------------------------ */
/* Models — registry view                                             */
/* ------------------------------------------------------------------ */

async function initModels() {
  const table = document.querySelector("table");
  try {
    const rows = await api("/models");
    makeSortable(table, rows,
      [
        { label: "Model", sort: (m) => m.name },
        { label: "Task" },
        { label: "Versions", sort: (m) => m.version_count },
        { label: "Production" },
        { label: "Key metric", sort: (m) => bestMetric({ metrics: m.production_version?.metrics })?.value },
      ],
      (m) => {
        const prod = m.production_version;
        const best = prod ? bestMetric({ metrics: prod.metrics }) : null;
        return el("tr", {},
          el("td", {}, el("a", { href: `/models/${m.id}` }, m.name)),
          el("td", { class: "muted" }, m.task || "—"),
          el("td", { class: "num" }, String(m.version_count)),
          el("td", {}, prod ? el("span", {}, statusBadge("PRODUCTION"), ` v${prod.version_number}`)
                            : el("span", { class: "faint" }, "none")),
          el("td", { class: "num" }, best ? `${best.name} ${fmt.metric(best.value)}` : "—"));
      });
  } catch (e) {
    setError(table.parentElement, e);
  }
}

async function initModelDetail(id) {
  const head = document.getElementById("model-head");
  const body = document.getElementById("model-body");
  try {
    const [model, versions] = await Promise.all([
      api(`/models/${id}`),
      api(`/models/${id}/versions`),
    ]);

    mount(head,
      el("div", { class: "breadcrumb" }, el("a", { href: "/models" }, "Models"), " / ", model.name),
      el("h2", {}, model.name),
      el("p", { class: "subtitle" }, model.description || "No description",
        model.task ? el("span", { class: "faint" }, `  ·  ${model.task}`) : null));

    const reconcilePanel = el("div", {});
    const ordered = versions.slice().reverse();
    const metricKeys = [...new Set(versions.flatMap((v) => Object.keys(v.metrics || {})))]
      .filter((k) => METRIC_PRIORITY.includes(k))
      .sort((a, b) => METRIC_PRIORITY.indexOf(a) - METRIC_PRIORITY.indexOf(b));

    const table = el("table", {},
      el("thead", {}, el("tr", {},
        el("th", {}, "Version"), el("th", {}, "State"),
        ...metricKeys.map((k) => el("th", {}, k)),
        el("th", {}, "Run"), el("th", {}, "Dataset"), el("th", {}, "Created"))),
      el("tbody", {}, ...(ordered.length ? ordered.map((v) => {
        const best = {};
        for (const k of metricKeys) {
          best[k] = Math.max(...versions.map((x) => (x.metrics || {})[k] ?? -Infinity));
        }
        return el("tr", {},
          el("td", {}, el("strong", {}, `v${v.version_number}`)),
          el("td", {}, statusBadge(v.state)),
          ...metricKeys.map((k) => {
            const val = (v.metrics || {})[k];
            const isBest = typeof val === "number" && val === best[k] && versions.length > 1;
            return el("td", { class: "num" },
              isBest ? el("strong", { style: "color:var(--ok)" }, fmt.metric(val)) : fmt.metric(val));
          }),
          el("td", {}, v.training_run_id ? el("a", { href: `/runs/${v.training_run_id}` }, `#${v.training_run_id}`) : "—"),
          el("td", { class: "muted" }, v.dataset_version_id ? `#${v.dataset_version_id}` : "—"),
          el("td", { class: "muted nowrap" }, fmt.ago(v.created_at)));
      }) : [emptyRow(metricKeys.length + 5, "No versions registered yet.")])));

    const prod = versions.find((v) => v.state === "PRODUCTION");
    const chart = metricKeys.length && versions.length > 1
      ? barChart(`${metricKeys[0]} by version`, ordered.map((v) => ({
          label: `v${v.version_number}`,
          value: (v.metrics || {})[metricKeys[0]] ?? 0,
          kind: v.state === "PRODUCTION" ? "success" : "",
        })))
      : null;

    mount(body,
      prod ? el("div", { class: "card", style: "margin-bottom:16px" },
        el("div", { class: "chart-title" }, "Current production version"),
        el("dl", { class: "kv" },
          el("dt", {}, "Version"), el("dd", {}, `v${prod.version_number}`),
          el("dt", {}, "Artifact"), el("dd", {}, prod.artifact_uri || "—"),
          el("dt", {}, "Training run"), el("dd", {}, prod.training_run_id ? `#${prod.training_run_id}` : "—"),
          el("dt", {}, "Promoted"), el("dd", {}, fmt.time(prod.created_at)))) : null,
      el("h3", {}, "Versions"),
      el("div", { class: "table-wrap" }, table),
      chart ? el("div", { style: "margin-top:16px;max-width:520px" }, chart) : null,
      prod ? el("p", { style: "margin-top:16px" },
        el("a", { class: "btn", href: `/lineage?kind=model-version&id=${prod.id}` }, "View lineage")) : null,
      reconcilePanel);

    renderRegistryReconciliation(reconcilePanel, id);
  } catch (e) {
    setError(body, e);
  }
}

/* ------------------------------------------------------------------ */
/* Lineage                                                             */
/* ------------------------------------------------------------------ */

async function renderLineagePicker(out) {
  // The nav's "Lineage" link (and any deep-link with no ?kind=&id=) used
  // to land here with nothing but instructions — technically correct
  // (a graph needs a root to walk from) but useless the moment someone
  // actually wants to see a lineage, since every other page reaches this
  // one only via a link that already carries a node. Listing the current
  // production model versions and latest dataset versions turns this
  // into a real landing page instead of a dead end.
  let models, datasets;
  try {
    [models, datasets] = await Promise.all([api("/models"), api("/datasets")]);
  } catch (e) {
    setError(out, e);
    return;
  }

  const modelRows = models
    .filter((m) => m.production_version)
    .map((m) => el("tr", {},
      el("td", {},
        el("a", { href: `/lineage?kind=model-version&id=${m.production_version.id}` },
          `${m.name} — production v${m.production_version.version_number}`)),
      el("td", { class: "muted" }, "ModelVersion")));

  const datasetRows = datasets
    .filter((d) => d.latest_version)
    .map((d) => el("tr", {},
      el("td", {},
        el("a", { href: `/lineage?kind=dataset-version&id=${d.latest_version.id}` },
          `${d.name} — v${d.latest_version.version_number}`)),
      el("td", { class: "muted" }, "DatasetVersion")));

  const rows = [...modelRows, ...datasetRows];
  if (rows.length === 0) {
    out.replaceChildren(banner(
      "Nothing to trace yet — lineage starts once a dataset has a version "
      + "and a model has a production version. Register a dataset and run "
      + "a training pipeline, then come back here."));
    return;
  }

  out.replaceChildren(
    el("p", { class: "muted" }, "Pick a starting point to walk its lineage:"),
    el("div", { class: "table-wrap" },
      el("table", {},
        el("thead", {}, el("tr", {}, el("th", {}, "Start from"), el("th", {}, "Type"))),
        el("tbody", {}, ...rows))));
}

async function initLineage() {
  const params = new URLSearchParams(location.search);
  const kind = params.get("kind");
  const id = params.get("id");
  const out = document.getElementById("lineage-out");

  if (!kind || !id) {
    await renderLineagePicker(out);
    return;
  }
  try {
    const g = await api(`/lineage/${kind}/${id}`);
    const byId = new Map(g.nodes.map((n) => [n.id, n]));
    const order = ["Dataset", "DatasetVersion", "TrainingRun", "ModelVersion", "Model", "ServingInstance"];
    const chain = g.nodes.slice().sort(
      (a, b) => order.indexOf(a.type) - order.indexOf(b.type));

    const chainEl = el("div", { class: "lineage-chain" });
    chain.forEach((n, i) => {
      if (i) chainEl.appendChild(el("div", { class: "lineage-arrow" }, "→"));
      chainEl.appendChild(
        el("div", { class: `lineage-node ${n.type}` },
          el("div", { class: "type" }, n.type),
          el("div", { class: "label" }, n.label || n.id)));
    });

    out.replaceChildren(
      el("div", { class: "card" }, chainEl),
      el("h3", {}, `Edges (${g.edges.length})`),
      el("div", { class: "table-wrap" },
        el("table", {},
          el("thead", {}, el("tr", {}, el("th", {}, "From"), el("th", {}, "Relation"), el("th", {}, "To"))),
          el("tbody", {}, ...g.edges.map((e) =>
            el("tr", {},
              el("td", { class: "mono" }, byId.get(e.source)?.label || e.source),
              el("td", { class: "muted" }, e.type),
              el("td", { class: "mono" }, byId.get(e.target)?.label || e.target)))))));
  } catch (e) {
    setError(out, e);
  }
}

/* ------------------------------------------------------------------ */
/* Experiments (MLflow)                                                */
/* ------------------------------------------------------------------ */

async function initExperiments() {
  const out = document.getElementById("experiments-out");
  let p;
  try {
    p = await api("/mlflow/experiments");
  } catch (e) {
    setError(out, e);
    return;
  }
  if (!p.available) {
    out.replaceChildren(banner(p.reason, "warn"));
    return;
  }

  // Deleted experiments stay in MLflow's store; showing them alongside the
  // live ones would misrepresent what is actually being tracked.
  const rows = (p.data.experiments || []).filter((e) => e.lifecycle_stage === "active");
  const table = el("table", {}, el("thead", {}, el("tr", {})), el("tbody", {}));
  out.replaceChildren(el("div", { class: "table-wrap" }, table));

  makeSortable(table, rows,
    [
      { label: "Experiment", sort: (e) => e.name },
      { label: "ID", sort: (e) => e.experiment_id },
      { label: "Artifact location" },
      { label: "Created", sort: (e) => e.creation_time },
    ],
    (e) => el("tr", {},
      el("td", {}, el("a", { href: `/experiments/${encodeURIComponent(e.experiment_id)}` }, e.name)),
      el("td", { class: "mono" }, e.experiment_id),
      el("td", { class: "mono faint truncate", title: e.artifact_location || "" },
        e.artifact_location || "—"),
      el("td", { class: "muted nowrap" }, fmt.ago(e.creation_time))));
}

async function initExperimentDetail(experimentId) {
  const head = document.getElementById("exp-head");
  const body = document.getElementById("exp-body");
  const rankBy = document.getElementById("rank-by");
  const rankDir = document.getElementById("rank-dir");
  const filterInput = document.getElementById("filter-string");

  mount(head,
    el("div", { class: "breadcrumb" },
      el("a", { href: "/experiments" }, "Experiments"), " / ", experimentId),
    el("h2", {}, `Experiment ${experimentId}`),
    el("p", { class: "subtitle" },
      "Runs ranked server-side by MLflow. Runs this framework started link back to their training run."));

  // Framework runs carry the MLflow run id, so a leaderboard row can point
  // back at the run that produced it.
  let byMlflowId = new Map();
  try {
    const runs = await api("/training-runs?limit=500");
    byMlflowId = new Map(runs.filter((r) => r.mlflow_run_id).map((r) => [r.mlflow_run_id, r.id]));
  } catch { /* the cross-link is a bonus, not a requirement */ }

  let metricsSeen = null;

  async function load() {
    body.replaceChildren(el("div", { class: "card muted" }, "Loading…"));
    const params = new URLSearchParams({ limit: "100", direction: rankDir.value });
    if (rankBy.value) params.set("order_by", rankBy.value);
    if (filterInput.value.trim()) params.set("filter_string", filterInput.value.trim());

    let p;
    try {
      p = await api(`/mlflow/experiments/${encodeURIComponent(experimentId)}/runs?${params}`);
    } catch (e) {
      setError(body, e);
      return;
    }
    if (!p.available) {
      body.replaceChildren(banner(p.reason, "warn"));
      return;
    }

    const runs = p.data.runs || [];
    const metricKeys = [...new Set(runs.flatMap((r) => Object.keys(r.metrics || {})))].sort();

    // Populate the rank-by choices once, from what the runs actually have.
    if (metricsSeen === null && metricKeys.length) {
      metricsSeen = metricKeys;
      rankBy.replaceChildren(
        el("option", { value: "" }, "start time"),
        ...metricKeys.map((k) => el("option", { value: k }, k)));
      const preferred = METRIC_PRIORITY.find((m) => metricKeys.includes(m));
      if (preferred) {
        rankBy.value = preferred;
        load();
        return;
      }
    }

    const shown = metricKeys.filter((k) => METRIC_PRIORITY.includes(k) || k === rankBy.value);
    const best = {};
    for (const k of shown) {
      best[k] = Math.max(...runs.map((r) => (r.metrics || {})[k] ?? -Infinity));
    }

    const table = el("table", {},
      el("thead", {}, el("tr", {},
        el("th", {}, "#"), el("th", {}, "Run"), el("th", {}, "Status"),
        ...shown.map((k) => el("th", {}, k)),
        el("th", {}, "Training run"), el("th", {}, "Started"))),
      el("tbody", {}, ...(runs.length ? runs.map((r, i) => {
        const fwId = byMlflowId.get(r.run_id);
        return el("tr", {},
          el("td", { class: "num" }, String(i + 1)),
          el("td", {}, el("span", { class: "mono", title: r.run_id },
            r.run_name || r.run_id.slice(0, 8))),
          el("td", {}, statusBadge(r.status)),
          ...shown.map((k) => {
            const v = (r.metrics || {})[k];
            const isBest = typeof v === "number" && v === best[k] && runs.length > 1;
            return el("td", { class: "num" },
              isBest ? el("strong", { style: "color:var(--ok)" }, fmt.metric(v)) : fmt.metric(v));
          }),
          el("td", {}, fwId ? el("a", { href: `/runs/${fwId}` }, `#${fwId}`)
                            : el("span", { class: "faint" }, "—")),
          el("td", { class: "muted nowrap" }, fmt.ago(r.start_time)));
      }) : [emptyRow(shown.length + 5, "No runs matched.")])));

    const headline = rankBy.value || METRIC_PRIORITY.find((m) => shown.includes(m));
    const chart = headline && runs.length > 1
      ? barChart(`${headline} by run`, runs.slice(0, 12).map((r) => ({
          label: r.run_name || r.run_id.slice(0, 6),
          value: (r.metrics || {})[headline] ?? 0,
        })))
      : null;

    mount(body,
      el("p", { class: "muted", style: "margin:0 0 10px" },
        `${runs.length} run${runs.length === 1 ? "" : "s"} · ordered by ${p.data.order_by}`),
      el("div", { class: "table-wrap" }, table),
      chart ? el("div", { style: "margin-top:16px;max-width:560px" }, chart) : null);
  }

  document.getElementById("apply-rank").addEventListener("click", load);
  rankBy.addEventListener("change", load);
  rankDir.addEventListener("change", load);
  filterInput.addEventListener("keydown", (e) => { if (e.key === "Enter") load(); });
  await load();
}

/* ------------------------------------------------------------------ */
/* MLflow model registry reconciliation                                */
/* ------------------------------------------------------------------ */

// The framework promotes versions in its own table; MLflow keeps a registry
// of its own; nothing reconciles them. A version marked PRODUCTION here
// with no alias in MLflow means one of the two sides never blessed what is
// actually being served — this panel is the only place that surfaces it.
function renderRegistryReconciliation(host, modelId) {
  api(`/models/${modelId}/registry-reconciliation`).then((p) => {
    if (!p.available) {
      host.replaceChildren(
        el("h3", {}, "MLflow registry"), banner(p.reason, "warn"));
      return;
    }
    const d = p.data;
    const rows = d.versions || [];

    const table = el("table", {},
      el("thead", {}, el("tr", {},
        el("th", {}, "Version"),
        el("th", {}, "Framework state"),
        el("th", {}, "In MLflow registry"),
        el("th", {}, "MLflow version"),
        el("th", {}, "Stage"),
        el("th", {}, "Aliases"),
        el("th", {}, ""))),
      el("tbody", {}, ...(rows.length ? rows.map((v) =>
        el("tr", {},
          el("td", {}, el("strong", {}, `v${v.framework_version_number}`)),
          el("td", {}, statusBadge(v.framework_state)),
          el("td", {}, v.in_mlflow_registry
            ? el("span", { class: "badge success" }, "yes")
            : el("span", { class: "faint" }, "no")),
          el("td", { class: "mono" }, v.mlflow_version || "—"),
          // MLflow 3 deprecated stages, so "None" here is normal and only
          // meaningful next to the alias column.
          el("td", { class: "mono faint" }, v.mlflow_stage || "—"),
          el("td", { class: "mono" },
            (v.mlflow_aliases || []).length ? v.mlflow_aliases.join(", ")
                                            : el("span", { class: "faint" }, "—")),
          el("td", {}, v.drift
            ? el("span", { class: "badge failed", title: v.drift_reason || "" }, "drift")
            : el("span", { class: "faint" }, "ok")))
      ) : [emptyRow(7, "No versions to reconcile.")])));

    const drifted = rows.filter((v) => v.drift);
    mount(host,
      el("div", { class: "section-head" },
        el("h3", {}, "MLflow registry"),
        el("span", { class: "faint" },
          d.registry_names?.length
            ? `registered as ${d.registry_names.join(", ")}`
            : "no matching registered model")),
      d.drift_count
        ? banner(
            `${d.drift_count} version${d.drift_count > 1 ? "s" : ""} disagree ` +
            `between this framework and MLflow: ` +
            drifted.map((v) => `v${v.framework_version_number} — ${v.drift_reason}`).join("; "),
            "err")
        : banner("Framework state and MLflow registry agree on every version."),
      el("div", { class: "table-wrap" }, table),
      (d.mlflow_only || []).length
        ? el("div", { style: "margin-top:16px" },
            el("div", { class: "chart-title", style: "margin-bottom:8px" },
              "Registered in MLflow but unknown to this framework"),
            el("div", { class: "table-wrap" },
              el("table", {},
                el("thead", {}, el("tr", {},
                  el("th", {}, "Name"), el("th", {}, "Version"),
                  el("th", {}, "Run"), el("th", {}, "Aliases"))),
                el("tbody", {}, ...d.mlflow_only.map((m) =>
                  el("tr", {},
                    el("td", { class: "mono" }, m.mlflow_name),
                    el("td", { class: "mono" }, m.mlflow_version),
                    el("td", { class: "mono truncate", title: m.run_id }, m.run_id),
                    el("td", { class: "mono" }, (m.aliases || []).join(", ") || "—")))))))
        : null);
  }).catch(() => {});
}

/* ------------------------------------------------------------------ */
/* Pipelines (Airflow)                                                 */
/* ------------------------------------------------------------------ */

function airflowHealthCard(data) {
  const health = data.health || {};
  const importErrors = data.import_errors || [];
  const pools = data.pools || [];

  const components = Object.entries(health).map(([name, info]) => {
    const status = (info && info.status) || null;
    const kind = status === "healthy" ? "ok" : status ? "err" : "";
    return el("div", { class: `kpi ${kind}` },
      el("div", { class: "label" }, name.replace(/_/g, " ")),
      el("div", { class: "value" }, status || "n/a"));
  });

  const poolRows = pools.map((p) =>
    el("div", { class: "kpi" },
      el("div", { class: "label" }, p.name),
      el("div", { class: "value" }, `${p.running_slots}/${p.slots}`)));

  return el("div", {},
    components.length || poolRows.length
      ? el("div", { class: "kpi-grid", style: "margin-bottom:12px" }, ...components, ...poolRows)
      : null,
    importErrors.length
      ? el("div", { class: "banner err", style: "margin-bottom:12px" },
          el("strong", {}, `${importErrors.length} DAG file${importErrors.length > 1 ? "s" : ""} failed to parse: `),
          importErrors.map((e) => e.filename).join(", "))
      : null);
}

async function initPipelines() {
  const healthHost = document.getElementById("pipelines-health");
  const out = document.getElementById("pipelines-out");

  api("/airflow/health").then((p) => {
    if (!p.available) { healthHost.replaceChildren(banner(p.reason, "warn")); return; }
    healthHost.replaceChildren(airflowHealthCard(p.data));
  }).catch(() => {});

  let p;
  try {
    p = await api("/airflow/dags");
  } catch (e) {
    setError(out, e);
    return;
  }
  if (!p.available) {
    out.replaceChildren(banner(p.reason, "warn"));
    return;
  }

  const dags = p.data.dags || [];
  const table = el("table", {}, el("thead", {}, el("tr", {})), el("tbody", {}));
  out.replaceChildren(el("div", { class: "table-wrap" }, table));

  makeSortable(table, dags,
    [
      { label: "DAG", sort: (d) => d.dag_id },
      { label: "Status" },
      { label: "Schedule" },
      { label: "Next run", sort: (d) => d.next_dagrun },
      { label: "Owners" },
      { label: "Tags" },
    ],
    (d) => el("tr", {},
      el("td", {}, el("a", { href: `/pipelines/${encodeURIComponent(d.dag_id)}` }, d.dag_id)),
      el("td", {}, el("span", { class: `badge ${d.is_paused ? "cancelled" : "success"}` },
        d.is_paused ? "Paused" : "Active")),
      el("td", { class: "mono faint" }, d.schedule_interval || "manual / external only"),
      el("td", { class: "muted nowrap" }, d.next_dagrun ? fmt.time(d.next_dagrun) : "—"),
      el("td", { class: "muted" }, (d.owners || []).join(", ") || "—"),
      el("td", { class: "muted" }, (d.tags || []).join(", ") || "—")));
}

// Layers `tasks` into columns for a general DAG graph view: level(root)
// = 0, level(n) = 1 + max(level(upstream)) over every incoming edge —
// Kahn's topological order, so a node is only placed once every one of
// its upstreams already has a level (a plain BFS-from-roots would
// under-place a join node fed by branches of different lengths).
// A single unbranched chain is just the special case where every
// column holds exactly one task, so this replaces the old
// linear-only chain renderer rather than sitting beside it.
//
// Returns an array of columns (each an array of task_id), or null if
// an edge points at an unknown task_id or a cycle leaves some node
// unplaceable — either means the structure can't be trusted enough to
// draw, and the caller falls back to the plain task table.
function dagLevels(tasks) {
  const byId = new Map(tasks.map((t) => [t.task_id, t]));
  const indegree = new Map(tasks.map((t) => [t.task_id, 0]));
  for (const t of tasks) {
    for (const d of t.downstream_task_ids || []) {
      if (!byId.has(d)) return null;
      indegree.set(d, (indegree.get(d) || 0) + 1);
    }
  }

  const level = new Map();
  const remaining = new Map(indegree);
  const placed = new Set();
  let frontier = tasks.filter((t) => (indegree.get(t.task_id) || 0) === 0).map((t) => t.task_id);
  if (frontier.length === 0 && tasks.length > 0) return null;
  frontier.forEach((id) => { level.set(id, 0); placed.add(id); });

  while (frontier.length) {
    const next = [];
    for (const id of frontier) {
      for (const d of byId.get(id).downstream_task_ids || []) {
        level.set(d, Math.max(level.get(d) || 0, level.get(id) + 1));
        remaining.set(d, remaining.get(d) - 1);
        if (remaining.get(d) === 0 && !placed.has(d)) {
          placed.add(d);
          next.push(d);
        }
      }
    }
    frontier = next;
  }
  if (placed.size !== tasks.length) return null;

  const columns = [];
  for (const t of tasks) {
    const lvl = level.get(t.task_id) || 0;
    (columns[lvl] || (columns[lvl] = [])).push(t.task_id);
  }
  return columns;
}

// Renders `tasks` as a layered graph: node positions come straight from
// (column, row) on a fixed grid — computed directly rather than
// measured from the DOM after paint, the same approach lineChart/
// barChart already use below — with a single SVG overlay drawing one
// curve per downstream edge between those same computed points, so
// edges can never drift out of sync with the nodes they connect.
// `stateByTaskId` (task_id -> Airflow state string) is optional; when
// given, nodes are coloured by it (the console passes the latest run's
// states), otherwise nodes render neutral.
function renderDagGraph(tasks, levels, stateByTaskId) {
  const COL_W = 210, ROW_H = 72, NODE_W = 180, NODE_H = 44, PAD = 16;
  const byId = new Map(tasks.map((t) => [t.task_id, t]));
  const pos = new Map();
  levels.forEach((col, ci) => {
    col.forEach((tid, ri) => pos.set(tid, { x: PAD + ci * COL_W, y: PAD + ri * ROW_H }));
  });
  const maxRows = Math.max(1, ...levels.map((c) => c.length));
  const width = PAD * 2 + (levels.length - 1) * COL_W + NODE_W;
  const height = PAD * 2 + (maxRows - 1) * ROW_H + NODE_H;

  const svg = svgEl("svg", {
    style: `position:absolute;inset:0;width:${width}px;height:${height}px`,
    viewBox: `0 0 ${width} ${height}`,
  });
  for (const t of tasks) {
    const from = pos.get(t.task_id);
    for (const d of t.downstream_task_ids || []) {
      const to = pos.get(d);
      if (!from || !to) continue;
      const x1 = from.x + NODE_W, y1 = from.y + NODE_H / 2;
      const x2 = to.x, y2 = to.y + NODE_H / 2;
      const midX = (x1 + x2) / 2;
      svg.appendChild(svgEl("path", {
        class: "dag-edge",
        d: `M${x1},${y1} C${midX},${y1} ${midX},${y2} ${x2},${y2}`,
        fill: "none",
      }));
    }
  }

  const nodes = tasks.map((t) => {
    const p = pos.get(t.task_id);
    const state = stateByTaskId && stateByTaskId.get(t.task_id);
    const kind = state ? statusKind(state) : "";
    return el("div", {
      class: `lineage-node Task${kind ? ` state-${kind}` : ""}`,
      style: `position:absolute;left:${p.x}px;top:${p.y}px;width:${NODE_W}px`,
      title: state ? `${t.task_id} — ${state}` : t.task_id,
    },
      el("div", { class: "type" }, t.operator_name || "task"),
      el("div", { class: "label" }, t.task_id));
  });

  return el("div",
    { class: "dag-graph", style: `position:relative;width:${width}px;height:${height}px` },
    svg, ...nodes);
}

// Airflow-Tree-View-style grid: one row per task (declaration order),
// one column per run (newest first, matching the "Recent runs" table
// below it). Cells come from the task-instance data `/airflow/dags/
// {id}` already expanded server-side (`grid_cells`) — no per-cell
// request. A cell only links to the framework's own run page when
// `byExecutionId` resolves one; a scheduler-triggered run has no
// framework-side row to link to, same distinction "Recent runs" draws.
function renderTaskHistoryGrid(tasks, gridRunIds, gridCells, byExecutionId, dagId) {
  if (!gridRunIds.length) {
    return banner("No run history yet for this DAG.");
  }
  const byTaskThenRun = new Map();
  for (const c of gridCells) {
    if (!byTaskThenRun.has(c.task_id)) byTaskThenRun.set(c.task_id, new Map());
    byTaskThenRun.get(c.task_id).set(c.dag_run_id, c);
  }

  const header = el("tr", {},
    el("th", {}, "Task"),
    ...gridRunIds.map((rid) => el("th", { class: "mono" }, rid.replace(/^mlops-/, ""))));

  const rows = (tasks.length ? tasks : [...byTaskThenRun.keys()].map((task_id) => ({ task_id })))
    .map((t) => {
      const byRun = byTaskThenRun.get(t.task_id) || new Map();
      return el("tr", {},
        el("td", { class: "mono" }, t.task_id),
        ...gridRunIds.map((rid) => {
          const cell = byRun.get(rid);
          if (!cell) return el("td", {}, el("span", { class: "tree-cell" }));
          const kind = statusKind(cell.state);
          const fwId = byExecutionId.get(`${dagId}/${rid}`);
          const title = `${cell.state}`
            + (cell.duration != null ? ` · ${cell.duration.toFixed(1)}s` : "")
            + ` · ${rid}`;
          const swatch = el("span", { class: `tree-cell ${kind}`, title });
          return el("td", {}, fwId
            ? el("a", { class: "tree-cell-link", href: `/runs/${fwId}`, "aria-label": title }, swatch)
            : swatch);
        }));
    });

  return el("div", { class: "table-wrap" },
    el("table", { class: "tree-grid" },
      el("thead", {}, header),
      el("tbody", {}, ...rows)));
}

async function initPipelineDetail(dagId) {
  const head = document.getElementById("pipeline-head");
  const body = document.getElementById("pipeline-body");

  mount(head,
    el("div", { class: "breadcrumb" }, el("a", { href: "/pipelines" }, "Pipelines"), " / ", dagId),
    el("h2", { class: "mono" }, dagId));

  // Runs this framework itself started carry the composite execution id
  // "dag_id/dag_run_id" — cross-linking back to them is what tells "a
  // scheduler-triggered run" and "a run this console already knows about"
  // apart in the history table below.
  let byExecutionId = new Map();
  try {
    const runs = await api("/training-runs?limit=500");
    byExecutionId = new Map(
      runs.filter((r) => r.execution_id).map((r) => [r.execution_id, r.id]));
  } catch { /* the cross-link is a bonus, not a requirement */ }

  let p;
  try {
    p = await api(`/airflow/dags/${encodeURIComponent(dagId)}`);
  } catch (e) {
    setError(body, e);
    return;
  }
  if (!p.available) {
    body.replaceChildren(banner(p.reason, "warn"));
    return;
  }

  const tasks = p.data.tasks || [];
  const dagRuns = p.data.dag_runs || [];
  const gridRunIds = p.data.grid_run_ids || [];
  const gridCells = p.data.grid_cells || [];

  // Colour the graph by the newest run in the grid — "no data yet" for
  // a DAG that has never run renders every node neutral.
  const latestStateByTask = new Map();
  if (gridRunIds.length) {
    for (const c of gridCells) {
      if (c.dag_run_id === gridRunIds[0]) latestStateByTask.set(c.task_id, c.state);
    }
  }
  const levels = tasks.length ? dagLevels(tasks) : null;
  const graphView = levels ? renderDagGraph(tasks, levels, latestStateByTask) : null;

  const taskTable = el("table", {},
    el("thead", {}, el("tr", {},
      el("th", {}, "Task"), el("th", {}, "Operator"),
      el("th", {}, "Trigger rule"), el("th", {}, "Downstream"))),
    el("tbody", {}, ...(tasks.length ? tasks.map((t) =>
      el("tr", {},
        el("td", { class: "mono" }, t.task_id),
        el("td", { class: "muted" }, t.operator_name || "—"),
        el("td", { class: "muted" }, t.trigger_rule || "—"),
        el("td", {}, (t.downstream_task_ids || []).join(", ") || "—")))
      : [emptyRow(4, "No tasks — the DAG may have failed to parse.")])));

  const runsTable = el("table", {},
    el("thead", {}, el("tr", {},
      el("th", {}, "Run"), el("th", {}, "Status"), el("th", {}, "Type"),
      el("th", {}, "Training run"), el("th", {}, "Started"), el("th", {}, "Ended"))),
    el("tbody", {}, ...(dagRuns.length ? dagRuns.map((r) => {
      const fwId = byExecutionId.get(`${dagId}/${r.dag_run_id}`);
      return el("tr", {},
        el("td", { class: "mono" }, r.dag_run_id),
        el("td", {}, statusBadge(r.state)),
        el("td", { class: "muted" }, r.run_type || "—"),
        el("td", {}, fwId ? el("a", { href: `/runs/${fwId}` }, `#${fwId}`)
                          : el("span", { class: "faint" }, "scheduler")),
        el("td", { class: "muted nowrap" }, fmt.ago(r.start_date || r.execution_date)),
        el("td", { class: "muted nowrap" }, fmt.time(r.end_date)));
    }) : [emptyRow(6, "No runs yet.")])));

  mount(body,
    el("h3", {}, "Tasks"),
    el("div", { class: "card", style: "margin-bottom:16px" },
      graphView ? el("div", { class: "table-wrap", style: "margin-bottom:16px" }, graphView) : null,
      el("div", { class: "table-wrap" }, taskTable)),
    el("h3", {}, "Task history"),
    el("p", { class: "muted", style: "margin:0 0 10px" },
      `Most recent ${gridRunIds.length || 0} run${gridRunIds.length === 1 ? "" : "s"}, newest first.`),
    el("div", { class: "card", style: "margin-bottom:16px" },
      renderTaskHistoryGrid(tasks, gridRunIds, gridCells, byExecutionId, dagId)),
    el("h3", {}, "Recent runs"),
    el("p", { class: "muted", style: "margin:0 0 10px" },
      "Includes runs the scheduler triggered on its own, not only ones started from this console."),
    el("div", { class: "table-wrap" }, runsTable));
}
