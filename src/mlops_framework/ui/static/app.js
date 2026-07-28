// MLOps Management UI — vanilla JS, no framework.
// All API calls are JSON via fetch(); each page has a small init function.

const API = "/api";

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
    } else if (v !== null && v !== undefined) {
      e.setAttribute(k, v);
    }
  }
  for (const c of children) {
    if (c == null) continue;
    if (typeof c === "string") e.appendChild(document.createTextNode(c));
    else e.appendChild(c);
  }
  return e;
}

function badge(text, kind) {
  return el("span", { class: `badge ${kind || ""}` }, text);
}

function statusBadge(status) {
  const s = (status || "").toLowerCase();
  if (s === "success" || s === "ready" || s === "production") return badge(status, "success");
  if (s === "failed" || s === "rejected" || s === "not_ready") return badge(status, "failed");
  if (s === "running") return badge(status, "running");
  if (s === "pending" || s === "training") return badge(status, "pending");
  if (s === "cancelled" || s === "archived") return badge(status, "cancelled");
  if (s === "candidate") return badge(status, "candidate");
  if (s === "approved") return badge(status, "approved");
  return badge(status, "");
}

function fmtPct(x) {
  if (x == null) return "—";
  return (x * 100).toFixed(1) + "%";
}

function fmtNum(x) {
  if (x == null) return "—";
  return Number(x).toLocaleString();
}

function fmtTime(s) {
  if (!s) return "—";
  try {
    return new Date(s).toLocaleString();
  } catch (e) { return s; }
}

function shortHash(h, n = 12) {
  if (!h) return "—";
  return h.substring(0, n) + (h.length > n ? "…" : "");
}

// ---- Dashboard ----
async function initDashboard() {
  try {
    const d = await api("/dashboard");
    const grid = document.getElementById("kpi-grid");
    const items = [
      { label: "Datasets", value: d.datasets },
      { label: "Dataset versions", value: d.dataset_versions },
      { label: "Total runs", value: d.total_runs },
      { label: "Active runs", value: d.active_runs, kind: d.active_runs > 0 ? "warn" : "" },
      { label: "Successful runs", value: d.success_runs, kind: "ok" },
      { label: "Failed runs", value: d.failed_runs, kind: d.failed_runs > 0 ? "err" : "" },
      { label: "Models", value: d.models },
      { label: "In production", value: d.production_models, kind: "ok" },
      { label: "Run success rate", value: fmtPct(d.success_rate), kind: d.success_rate >= 0.8 ? "ok" : (d.success_rate >= 0.5 ? "warn" : "err") },
    ];
    for (const it of items) {
      grid.appendChild(
        el("div", { class: `kpi ${it.kind || ""}` },
          el("div", { class: "label" }, it.label),
          el("div", { class: "value" }, String(it.value))
        )
      );
    }
  } catch (e) {
    document.getElementById("kpi-grid").innerHTML =
      `<div class="empty">Failed to load dashboard: ${e.message}</div>`;
  }
}

// ---- Datasets list ----
async function initDatasets() {
  const tbody = document.querySelector("table tbody");
  try {
    const data = await api("/datasets");
    if (!data.length) {
      tbody.appendChild(el("tr", {}, el("td", { colspan: "4", class: "empty" }, "No datasets yet.")));
      return;
    }
    for (const ds of data) {
      tbody.appendChild(
        el("tr", {},
          el("td", {}, el("a", { href: `/datasets/${ds.id}` }, ds.name)),
          el("td", { class: "muted" }, ds.description || "—"),
          el("td", {}, String(ds.version_count)),
          el("td", {}, ds.latest_version ? `v${ds.latest_version.version_number} (${fmtNum(ds.latest_version.row_count)} rows)` : "—")
        )
      );
    }
  } catch (e) {
    tbody.appendChild(el("tr", {}, el("td", { colspan: "4", class: "empty" }, `Error: ${e.message}`)));
  }
}

// ---- Dataset detail ----
async function initDatasetDetail(id) {
  try {
    const ds = await api(`/datasets/${id}`);
    const versions = await api(`/datasets/${id}/versions`);
    document.getElementById("ds-name").textContent = ds.name;
    document.getElementById("ds-desc").textContent = ds.description || "—";
    const tbody = document.querySelector("table tbody");
    for (const v of versions) {
      let readiness = "—";
      try {
        const r = await api(`/readiness/${v.id}`);
        if (r) readiness = r.status;
      } catch (e) {}
      tbody.appendChild(
        el("tr", {},
          el("td", {}, `v${v.version_number}`),
          el("td", { class: "mono checksum" }, shortHash(v.checksum)),
          el("td", { class: "mono checksum" }, shortHash(v.schema_hash)),
          el("td", {}, fmtNum(v.row_count)),
          el("td", { class: "mono" }, v.storage_uri),
          el("td", {}, statusBadge(readiness)),
        )
      );
    }
    if (!versions.length) {
      tbody.appendChild(el("tr", {}, el("td", { colspan: "6", class: "empty" }, "No versions yet.")));
    }
  } catch (e) {
    document.getElementById("ds-name").textContent = "Error";
    document.getElementById("ds-desc").textContent = e.message;
  }
}

// ---- Runs list ----
async function initRuns() {
  const tbody = document.querySelector("table tbody");
  const status = document.getElementById("status-filter");
  const refresh = document.getElementById("refresh");
  async function load() {
    tbody.innerHTML = "";
    const qs = status.value ? `?status=${encodeURIComponent(status.value)}` : "";
    try {
      const data = await api("/training-runs" + qs);
      if (!data.length) {
        tbody.appendChild(el("tr", {}, el("td", { colspan: "5", class: "empty" }, "No runs.")));
        return;
      }
      for (const r of data) {
        tbody.appendChild(
          el("tr", {},
            el("td", {}, String(r.id)),
            el("td", {}, statusBadge(r.status)),
            el("td", {}, r.pipeline_id || "—"),
            el("td", {}, fmtTime(r.started_at || r.created_at)),
            el("td", {}, el("a", { href: `/runs/${r.id}` }, "view"))
          )
        );
      }
    } catch (e) {
      tbody.appendChild(el("tr", {}, el("td", { colspan: "5", class: "empty" }, `Error: ${e.message}`)));
    }
  }
  status.addEventListener("change", load);
  refresh.addEventListener("click", load);
  load();
}

// ---- Run detail ----
async function initRunDetail(id) {
  try {
    const r = await api(`/training-runs/${id}`);
    document.getElementById("run-id").textContent = r.id;
    document.getElementById("run-status").innerHTML = "";
    document.getElementById("run-status").appendChild(statusBadge(r.status));
    document.getElementById("run-pipeline").textContent = r.pipeline_id || "—";
    document.getElementById("run-dataset-version").textContent = r.dataset_version_id || "—";
    document.getElementById("run-started").textContent = fmtTime(r.started_at);
    document.getElementById("run-completed").textContent = fmtTime(r.completed_at);
    document.getElementById("run-error").textContent = r.error_message || "—";
    document.getElementById("run-params").textContent = r.parameters ? JSON.stringify(r.parameters, null, 2) : "—";
    document.getElementById("run-metrics").textContent = r.metrics ? JSON.stringify(r.metrics, null, 2) : "—";
    // Link to dataset version
    const dv = document.getElementById("run-dataset-version");
    if (r.dataset_version_id) {
      dv.innerHTML = "";
      dv.appendChild(el("a", { href: `/datasets/${dv.textContent}` }, String(r.dataset_version_id)));
      // Resolve to dataset id
      try {
        const v = await api(`/dataset-versions/${r.dataset_version_id}`);
        dv.innerHTML = "";
        dv.appendChild(el("a", { href: `/datasets/${v.dataset_id}` }, `Dataset #${v.dataset_id} v${v.version_number}`));
      } catch (e) {}
    }
  } catch (e) {
    document.getElementById("run-id").textContent = `Error: ${e.message}`;
  }
}

// ---- Models list ----
async function initModels() {
  const tbody = document.querySelector("table tbody");
  try {
    const data = await api("/models");
    if (!data.length) {
      tbody.appendChild(el("tr", {}, el("td", { colspan: "5", class: "empty" }, "No models yet.")));
      return;
    }
    for (const m of data) {
      tbody.appendChild(
        el("tr", {},
          el("td", {}, el("a", { href: `/models/${m.id}` }, m.name)),
          el("td", { class: "muted" }, m.task || "—"),
          el("td", {}, String(m.version_count)),
          el("td", {}, m.production_version ? `v${m.production_version.version_number}` : "—"),
          el("td", {}, m.production_version ? statusBadge("PRODUCTION") : badge("—", ""))
        )
      );
    }
  } catch (e) {
    tbody.appendChild(el("tr", {}, el("td", { colspan: "5", class: "empty" }, `Error: ${e.message}`)));
  }
}

// ---- Model detail ----
async function initModelDetail(id) {
  try {
    const m = await api(`/models/${id}`);
    document.getElementById("m-name").textContent = m.name;
    document.getElementById("m-task").textContent = m.task || "—";
    document.getElementById("m-desc").textContent = m.description || "—";
    document.getElementById("m-count").textContent = m.version_count;
    const tbody = document.querySelector("table tbody");
    const versions = await api(`/models/${id}/versions`);
    for (const v of versions) {
      tbody.appendChild(
        el("tr", {},
          el("td", {}, `v${v.version_number}`),
          el("td", {}, statusBadge(v.state)),
          el("td", {}, v.metrics ? JSON.stringify(v.metrics) : "—"),
          el("td", {}, v.dataset_version_id ? `v${v.dataset_version_id}` : "—"),
          el("td", {}, v.training_run_id ? `#${v.training_run_id}` : "—"),
          el("td", {}, el("a", { href: `/lineage?kind=model-version&id=${v.id}` }, "lineage"))
        )
      );
    }
  } catch (e) {
    document.getElementById("m-name").textContent = `Error: ${e.message}`;
  }
}

// ---- Lineage ----
async function initLineage() {
  const params = new URLSearchParams(location.search);
  const kind = params.get("kind");
  const id = params.get("id");
  const out = document.getElementById("lineage-out");
  if (!kind || !id) {
    out.textContent = "Pick a node from a model or dataset to view its lineage.";
    return;
  }
  try {
    const r = await api(`/lineage/${kind}/${id}`);
    out.textContent = renderLineage(r);
  } catch (e) {
    out.textContent = `Error: ${e.message}`;
  }
}

function renderLineage(g) {
  // Simple ASCII tree from nodes + edges. Edges are (source, target, type).
  const byId = new Map();
  for (const n of g.nodes) byId.set(n.id, n);
  const children = new Map();
  for (const n of g.nodes) children.set(n.id, []);
  for (const e of g.edges) {
    if (children.has(e.source)) children.get(e.source).push({ id: e.target, type: e.type });
  }
  const root = g.root_id;
  const lines = [];
  function walk(nodeId, depth, seen) {
    if (seen.has(nodeId)) {
      lines.push("  ".repeat(depth) + byId.get(nodeId).label + "  (cycle)");
      return;
    }
    const node = byId.get(nodeId);
    if (!node) return;
    const next = new Set(seen);
    next.add(nodeId);
    lines.push("  ".repeat(depth) + "• " + node.label + "  [" + node.type + "]");
    const kids = (children.get(nodeId) || []).slice().sort((a, b) => a.id.localeCompare(b.id));
    for (let i = 0; i < kids.length; i++) {
      const k = kids[i];
      const last = i === kids.length - 1;
      lines.push("  ".repeat(depth) + (last ? "└─ " : "├─ ") + "(" + k.type + ")");
      walk(k.id, depth + 1, next);
    }
  }
  walk(root, 0, new Set());
  return lines.join("\n");
}
