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

  // Airflow task grid — only rendered when the run actually ran there.
  const tasksPanel = el("div", {});
  sections.push(tasksPanel);
  api(`/training-runs/${id}/tasks`).then((p) => {
    if (!p.available) {
      if (run.orchestrator === "AirflowOrchestrator") {
        tasksPanel.replaceChildren(el("h3", {}, "Airflow tasks"), banner(p.reason, "warn"));
      }
      return;
    }
    const tasks = Object.entries(p.data.tasks || {});
    tasksPanel.replaceChildren(
      el("h3", {}, "Airflow tasks"),
      el("div", { class: "card" },
        el("div", { class: "mono faint", style: "margin-bottom:8px" }, p.data.execution_id),
        tasks.length
          ? el("div", { class: "task-grid" },
              ...tasks.map(([tid, state]) =>
                el("div", { class: `task-cell ${statusKind(state)}` },
                  el("span", { class: "dot" }), tid,
                  el("span", { class: "state" }, state))))
          : el("div", { class: "muted" }, "No task instances — the DAG has not been scheduled.")));
  }).catch(() => {});

  // MLflow metric history — the training curves the framework doesn't store.
  const mlPanel = el("div", {});
  sections.push(mlPanel);
  api(`/training-runs/${id}/metric-history`).then((p) => {
    if (!p.available) {
      if (run.mlflow_run_id) {
        mlPanel.replaceChildren(el("h3", {}, "MLflow"), banner(p.reason, "warn"));
      }
      return;
    }
    const hist = p.data.history || {};
    const charts = Object.entries(hist)
      .filter(([, series]) => series.length > 1)
      .map(([name, series]) => lineChart(name, series));

    mlPanel.replaceChildren(
      el("div", { class: "section-head" },
        el("h3", {}, "MLflow"),
        el("a", {
          class: "faint",
          href: `${p.data.tracking_uri}/#/experiments/0/runs/${p.data.mlflow_run_id}`,
          target: "_blank", rel: "noopener",
        }, "open in MLflow ↗")),
      charts.length
        ? el("div", { class: "grid-3" }, ...charts)
        : el("div", { class: "card" },
            el("div", { class: "muted" },
              "Metrics were logged once, so there is no series to plot. " +
              "Values are shown above."),
            el("dl", { class: "kv", style: "margin-top:10px" },
              ...Object.entries(p.data.metrics || {}).flatMap(([k, v]) => [
                el("dt", {}, k), el("dd", {}, fmt.metric(v))]))));
  }).catch(() => {});

  mount(body, ...sections);
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
        el("a", { class: "btn", href: `/lineage?kind=model-version&id=${prod.id}` }, "View lineage")) : null);
  } catch (e) {
    setError(body, e);
  }
}

/* ------------------------------------------------------------------ */
/* Lineage                                                             */
/* ------------------------------------------------------------------ */

async function initLineage() {
  const params = new URLSearchParams(location.search);
  const kind = params.get("kind");
  const id = params.get("id");
  const out = document.getElementById("lineage-out");

  if (!kind || !id) {
    out.replaceChildren(banner(
      "Open a lineage view from a model or dataset page — the link carries the node to walk from."));
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
