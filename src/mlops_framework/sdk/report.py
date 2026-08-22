"""Reproducibility report — the logic behind ``MLOpsProject.report()``.

Split out of ``sdk/project.py`` (which stays a thin façade) because this
one function reads back nearly every table the framework writes,
end-to-end, for a single ModelVersion:

    * Lineage — Dataset -> DatasetVersion -> TrainingRun -> ModelVersion
      -> ServingInstance(s), via the existing LineageManager.
    * The dataset version's own content/schema hashes — what "the same
      data" means for reproducing this exact result.
    * Metrics/params — the framework's own ``metrics_json``, plus live
      params/metrics from MLflow when a tracking server is configured
      and the run is still there (best-effort; never blocks the report).
    * The full governance decision trail — ReadinessEvaluation,
      DriftEvaluation, ModelPromotionEvent — rows that until now were
      only ever written, never read back out as a group.
    * AuditLog / GovernanceEvent rows touching this model version, its
      dataset version, or its training run — who (or what) acted on it,
      and what the framework detected along the way.

Nothing here is new data — every fact already lives in a row this
framework wrote elsewhere. This module's only job is composing what is
already there into something a paper, a thesis, or a teammate six
months from now can read on its own, without a live MLflow/Airflow tab
open next to it.

Output is a plain string (Markdown or a minimal self-contained HTML) —
no PDF library, no template engine, no new dependency.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from html import escape as _html_escape
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from mlops_framework.audit.manager import AuditManager
from mlops_framework.database.models.dataset import Dataset
from mlops_framework.database.models.dataset_version import DatasetVersion
from mlops_framework.database.models.drift_evaluation import DriftEvaluation
from mlops_framework.database.models.model import Model as ModelRow
from mlops_framework.database.models.model_promotion_event import ModelPromotionEvent
from mlops_framework.database.models.model_version import ModelVersion
from mlops_framework.database.models.readiness_evaluation import ReadinessEvaluation
from mlops_framework.database.models.retraining_decision import RetrainingDecision
from mlops_framework.database.models.training_run import TrainingRun
from mlops_framework.events.store import GovernanceEventStore
from mlops_framework.exceptions import ModelVersionNotFoundError
from mlops_framework.lineage.manager import LineageManager
from mlops_framework.tracking.mlflow_client import client_or_reason

_SUPPORTED_FORMATS = ("markdown", "html")


def _loads(raw: str | None) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _ts(dt: datetime | None) -> str:
    return dt.isoformat() if dt else "—"


def _enum_value(v: Any) -> str:
    return v.value if hasattr(v, "value") else str(v)


def _tri(v: bool | None) -> str:
    """Render a gate verdict that has three states, not two.

    ``None`` means the gate never ran — the workflow returned before
    reaching it, or none was configured. Rendering that as "no" would
    report a refusal that never happened.
    """
    if v is None:
        return "—"
    return "yes" if v else "no"


def build_report(
    session: Session, model_version_id: int, *, format: str = "markdown"
) -> str:
    """Compose a self-contained reproducibility report for one ModelVersion.

    Raises:
        ModelVersionNotFoundError: no such ModelVersion.
        ValueError: ``format`` is not ``"markdown"`` or ``"html"``.
    """
    if format not in _SUPPORTED_FORMATS:
        raise ValueError(
            f"Unsupported report format {format!r}; use one of {_SUPPORTED_FORMATS}"
        )

    mv = session.get(ModelVersion, model_version_id)
    if mv is None:
        raise ModelVersionNotFoundError(f"ModelVersion {model_version_id} not found")

    model = session.get(ModelRow, mv.model_id)
    dataset_version = session.get(DatasetVersion, mv.dataset_version_id)
    dataset = session.get(Dataset, dataset_version.dataset_id) if dataset_version else None
    run = session.get(TrainingRun, mv.training_run_id) if mv.training_run_id else None

    lineage = LineageManager(session).graph_for_model_version(mv.id).to_dict()

    readiness_rows: list[ReadinessEvaluation] = []
    drift_rows: list[DriftEvaluation] = []
    if dataset_version is not None:
        readiness_rows = list(
            session.execute(
                select(ReadinessEvaluation)
                .where(ReadinessEvaluation.dataset_version_id == dataset_version.id)
                .order_by(ReadinessEvaluation.created_at.desc())
            ).scalars().all()
        )
        drift_rows = list(
            session.execute(
                select(DriftEvaluation)
                .where(
                    (DriftEvaluation.reference_dataset_version_id == dataset_version.id)
                    | (DriftEvaluation.current_dataset_version_id == dataset_version.id)
                )
                .order_by(DriftEvaluation.created_at.desc())
            ).scalars().all()
        )

    # Every governed retraining attempt made on the same dataset version,
    # not only the one that produced this model. An attempt that was
    # refused before it ever trained is part of the answer to "how did
    # this model version come to be the one in production" — it is the
    # record of the alternatives that were considered and declined.
    decision_rows: list[RetrainingDecision] = []
    if dataset_version is not None:
        decision_rows = list(
            session.execute(
                select(RetrainingDecision)
                .where(
                    RetrainingDecision.dataset_version_id == dataset_version.id
                )
                .order_by(RetrainingDecision.id.desc())
            ).scalars().all()
        )

    promotion_rows = list(
        session.execute(
            select(ModelPromotionEvent)
            .where(ModelPromotionEvent.model_version_id == mv.id)
            .order_by(ModelPromotionEvent.created_at.desc())
        ).scalars().all()
    )

    audit_rows = AuditManager(session).list_entries(
        entity_type="ModelVersion", entity_id=mv.id, limit=50
    )

    # Alerts about the dataset version or training run this model came
    # from — a drift or a training failure upstream is still part of the
    # story of how this model version came to exist.
    alert_targets = [
        ("DatasetVersion", dataset_version.id if dataset_version else None),
        ("TrainingRun", run.id if run else None),
    ]
    store = GovernanceEventStore(session)
    alerts = [
        row
        for entity_type, entity_id in alert_targets
        if entity_id is not None
        for row in store.list_entries(entity_type=entity_type, entity_id=entity_id, limit=50)
    ]
    alerts.sort(key=lambda a: a.id, reverse=True)

    mlflow_params: dict[str, Any] = {}
    mlflow_metrics: dict[str, Any] = {}
    mlflow_note: str | None = None
    if mv.mlflow_run_id:
        client, reason = client_or_reason()
        if client is None:
            mlflow_note = reason
        else:
            try:
                mlflow_run = client.get_run(mv.mlflow_run_id)
                mlflow_params = dict(mlflow_run.data.params)
                mlflow_metrics = dict(mlflow_run.data.metrics)
            except Exception as exc:  # noqa: BLE001 - best-effort enrichment only
                mlflow_note = f"MLflow error: {exc}"

    ctx: dict[str, Any] = {
        "generated_at": datetime.now(UTC),
        "model": model,
        "model_version": mv,
        "metrics": _loads(mv.metrics_json) or {},
        "dataset": dataset,
        "dataset_version": dataset_version,
        "training_run": run,
        "lineage": lineage,
        "decision_rows": decision_rows,
        "readiness_rows": readiness_rows,
        "drift_rows": drift_rows,
        "promotion_rows": promotion_rows,
        "audit_rows": audit_rows,
        "alerts": alerts,
        "mlflow_params": mlflow_params,
        "mlflow_metrics": mlflow_metrics,
        "mlflow_note": mlflow_note,
    }
    return _render_html(ctx) if format == "html" else _render_markdown(ctx)


# ---------------------------------------------------------------------- #
# Markdown rendering
# ---------------------------------------------------------------------- #


def _md_table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return "_None recorded._\n"
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    for row in rows:
        out.append("| " + " | ".join(str(c).replace("\n", " ").replace("|", "\\|") for c in row) + " |")
    return "\n".join(out) + "\n"


def _render_markdown(ctx: dict[str, Any]) -> str:
    model: ModelRow | None = ctx["model"]
    mv: ModelVersion = ctx["model_version"]
    ds: Dataset | None = ctx["dataset"]
    dv: DatasetVersion | None = ctx["dataset_version"]
    run: TrainingRun | None = ctx["training_run"]

    lines: list[str] = []
    title = f"{model.name if model else 'model'} v{mv.version_number}"
    lines.append(f"# Reproducibility report — {title}")
    lines.append("")
    lines.append(f"Generated {ctx['generated_at'].isoformat()} by Gateflow.")
    lines.append("")

    lines.append("## Model version")
    lines.append("")
    lines.append(_md_table(
        ["Field", "Value"],
        [
            ["Model", model.name if model else "—"],
            ["Task", model.task if model and model.task else "—"],
            ["Version", f"v{mv.version_number} (id #{mv.id})"],
            ["State", _enum_value(mv.state)],
            ["Artifact URI", mv.artifact_uri or "—"],
            ["MLflow run ID", mv.mlflow_run_id or "—"],
            ["Created", _ts(mv.created_at)],
        ],
    ))

    lines.append("## Metrics")
    lines.append("")
    metrics = dict(ctx["metrics"])
    metrics.update({k: v for k, v in ctx["mlflow_metrics"].items() if k not in metrics})
    lines.append(_md_table(["Metric", "Value"], [[k, v] for k, v in sorted(metrics.items())]))
    if ctx["mlflow_params"]:
        lines.append("### Parameters (from MLflow)")
        lines.append("")
        lines.append(_md_table(
            ["Parameter", "Value"], [[k, v] for k, v in sorted(ctx["mlflow_params"].items())]
        ))
    elif ctx["mlflow_note"]:
        lines.append(f"_MLflow params unavailable: {ctx['mlflow_note']}_")
        lines.append("")

    lines.append("## Dataset")
    lines.append("")
    if dv is not None:
        lines.append(_md_table(
            ["Field", "Value"],
            [
                ["Dataset", f"{ds.name if ds else '—'} (#{ds.id if ds else '—'})"],
                ["Version", f"v{dv.version_number} (id #{dv.id})"],
                ["Row count", f"{dv.row_count:,}"],
                ["Storage URI", dv.storage_uri],
                ["Checksum (SHA-256)", f"`{dv.checksum}`"],
                ["Schema hash (SHA-256)", f"`{dv.schema_hash}`"],
            ],
        ))
    else:
        lines.append("_No dataset version on record._")
        lines.append("")

    lines.append("## Training run")
    lines.append("")
    if run is not None:
        lines.append(_md_table(
            ["Field", "Value"],
            [
                ["Run", f"#{run.id}"],
                ["Pipeline", run.pipeline_id or "—"],
                ["Status", _enum_value(run.status)],
                ["Started", _ts(run.started_at)],
                ["Completed", _ts(run.completed_at)],
            ],
        ))
    else:
        lines.append("_No training run on record._")
        lines.append("")

    lines.append("## Lineage")
    lines.append("")
    nodes = {n["id"]: n for n in ctx["lineage"].get("nodes", [])}
    lines.append(_md_table(
        ["Node", "Type", "Label"],
        [[n["id"], n["type"], n["label"]] for n in nodes.values()],
    ))
    edges = ctx["lineage"].get("edges", [])
    if edges:
        lines.append("")
        lines.append(_md_table(
            ["From", "To", "Relationship"],
            [[e["source"], e["target"], e["type"]] for e in edges],
        ))

    lines.append("## Governance decisions")
    lines.append("")
    lines.append(
        "_One row per governed retraining attempt on this dataset "
        "version. `—` in a gate column means the gate was never "
        "reached, which is not the same as a refusal._"
    )
    lines.append("")
    lines.append(_md_table(
        ["When", "Outcome", "Stopped at", "Reason", "Eligible", "Approved", "By"],
        [
            [
                _ts(d.created_at),
                _enum_value(d.outcome),
                d.blocked_at_step or "—",
                d.blocked_reason or "—",
                _tri(d.eligible),
                _tri(d.approved),
                d.approval_responder or "—",
            ]
            for d in ctx["decision_rows"]
        ],
    ))

    lines.append("## Readiness decisions")
    lines.append("")
    lines.append(_md_table(
        ["When", "Status", "Reasons"],
        [
            [_ts(r.created_at), _enum_value(r.status), "; ".join(_loads(r.reasons_json) or [])]
            for r in ctx["readiness_rows"]
        ],
    ))

    lines.append("## Drift evaluations")
    lines.append("")
    lines.append(_md_table(
        ["When", "Method", "Outcome", "Score", "Threshold"],
        [
            [_ts(d.created_at), d.method, _enum_value(d.outcome),
             f"{d.score:.4f}" if d.score is not None else "—",
             f"{d.threshold:.4f}" if d.threshold is not None else "—"]
            for d in ctx["drift_rows"]
        ],
    ))

    lines.append("## Promotion events")
    lines.append("")
    lines.append(_md_table(
        ["When", "Status", "Version"],
        [
            [_ts(p.created_at), _enum_value(p.status), f"v{p.model_version_number}"]
            for p in ctx["promotion_rows"]
        ],
    ))

    lines.append("## Audit trail (this model version)")
    lines.append("")
    lines.append(_md_table(
        ["When", "Actor", "Action"],
        [[_ts(a.created_at), a.actor, a.action] for a in ctx["audit_rows"]],
    ))

    lines.append("## Governance alerts (upstream dataset/training run)")
    lines.append("")
    lines.append(_md_table(
        ["When", "Severity", "Type", "Message"],
        [
            [_ts(al.created_at), _enum_value(al.severity), al.event_type, al.message]
            for al in ctx["alerts"]
        ],
    ))

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------- #
# HTML rendering — the markdown, wrapped, not a second templating pass
# ---------------------------------------------------------------------- #


def _render_html(ctx: dict[str, Any]) -> str:
    """A minimal, self-contained HTML document.

    Deliberately not a Markdown-to-HTML conversion (that would be a new
    dependency for one query param) — the Markdown tables read fine as
    preformatted text; this just wraps them in a page with a title and
    monospace body so opening the file in a browser is still readable.
    """
    mv: ModelVersion = ctx["model_version"]
    model: ModelRow | None = ctx["model"]
    title = f"{model.name if model else 'model'} v{mv.version_number} — reproducibility report"
    body = _render_markdown(ctx)
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        f"<title>{_html_escape(title)}</title>"
        "<style>body{font-family:system-ui,sans-serif;max-width:900px;margin:32px auto;"
        "padding:0 16px;line-height:1.5} pre{white-space:pre-wrap;font-family:inherit}</style>"
        f"</head><body><pre>{_html_escape(body)}</pre></body></html>"
    )
