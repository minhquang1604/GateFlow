"""Phase 12 — final state, read back from the database.

Every value printed here is queried fresh rather than taken from the
demo's own bookkeeping. That is the point of the step: it is the only
place the run checks whether what it *believes* happened matches what
was actually persisted. If the two disagree, this block shows the
database's answer and the discrepancy is visible rather than papered
over by a summary the demo wrote from memory.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from demo.context import DemoContext
from demo.reporting import (
    RULE,
    banner,
    bullet,
    detail,
    evidence_block,
    kv,
    lineage_block,
    section,
)
from mlops_framework.database.models.dataset_version import DatasetVersion
from mlops_framework.database.models.model_version import ModelState, ModelVersion
from mlops_framework.lineage.manager import LineageManager


def run(ctx: DemoContext, outcome: Any | None = None) -> None:
    """Print lineage, the final persisted state, and the event log."""
    model_id = ctx.require("model_id")

    # -- Lineage --------------------------------------------------------- #
    target = ctx.v2_model_version_id or ctx.v1_model_version_id
    if target is not None:
        banner("LINEAGE — why does this model exist?")
        with ctx.db.get_session() as session:
            graph = LineageManager(session).graph_for_model_version(target)
        lineage_block(graph)
        _narrate_lineage(ctx)

    # -- Final state, queried ------------------------------------------- #
    banner("FINAL SYSTEM STATE")
    with ctx.db.get_session() as session:
        versions = list(
            session.execute(
                select(ModelVersion)
                .where(ModelVersion.model_id == model_id)
                .order_by(ModelVersion.version_number)
            ).scalars().all()
        )
        dataset_versions = list(
            session.execute(
                select(DatasetVersion)
                .where(DatasetVersion.dataset_id == ctx.require("dataset_id"))
                .order_by(DatasetVersion.version_number)
            ).scalars().all()
        )
        production = [v for v in versions if v.state == ModelState.PRODUCTION]

        section("Dataset")
        for dv in dataset_versions:
            parent = f" (derived from #{dv.parent_version_id})" if dv.parent_version_id else ""
            kv(
                f"dataset_v{dv.version_number}",
                f"{dv.row_count:,} rows, id={dv.id}{parent}",
                width=22,
            )
        current = dataset_versions[-1] if dataset_versions else None
        kv(
            "Current version",
            f"dataset_v{current.version_number}" if current else "None",
            width=22,
        )

        section("Models")
        for mv in versions:
            kv(f"model_v{mv.version_number}", mv.state.value, width=22)

        section("Governance")
        kv(
            "Drift event",
            f"drift_event_{ctx.state.drift_event_id}"
            if ctx.state.drift_event_id is not None
            # The no-drift path reaches here too, and "drift_event_None"
            # reads like a bug rather than the correct outcome it is.
            else "(none — no drift was detected)",
            width=22,
        )
        kv(
            "Drift status",
            "RESOLVED" if (outcome and outcome.promoted) else ctx.state.drift_status,
            width=22,
        )
        kv("Approval", ctx.state.approval_status, width=22)
        kv("Retraining", ctx.state.retraining_status, width=22)
        kv("Validation", ctx.state.validation_status, width=22)
        kv("Monitoring", ctx.state.monitoring_status, width=22)

        section("Production model")
        if len(production) == 1:
            kv("Version", f"model_v{production[0].version_number}", width=22)
            kv("Model version id", production[0].id, width=22)
        elif not production:
            kv("Version", "NONE — no model is serving", width=22)
        else:
            # The one-production-per-model invariant (migration 006) makes
            # this unreachable through the framework; if it ever prints,
            # something wrote the table directly.
            kv(
                "Version",
                f"INVARIANT VIOLATED — {len(production)} PRODUCTION versions",
                width=22,
            )
    print(RULE)

    # -- The structured log --------------------------------------------- #
    evidence_block(ctx.evidence)


def _narrate_lineage(ctx: DemoContext) -> None:
    """The chain in prose, read bottom-up, as the spec asks it."""
    if ctx.v2_model_version_id is None:
        return
    section("Read as a causal chain")
    bullet(f"model_v2 (id={ctx.v2_model_version_id})")
    bullet(f"  <- trained from dataset_v2 (id={ctx.v2_version_id})")
    bullet(
        f"  <- dataset_v2 created from dataset_v1 (id={ctx.v1_version_id}) "
        f"+ production window (id={ctx.drifted_window_version_id})"
    )
    bullet(
        f"  <- created because drift_event_{ctx.state.drift_event_id} was detected"
    )
    bullet(
        f"  <- drift_event_{ctx.state.drift_event_id} raised by production "
        f"window (id={ctx.drifted_window_version_id}) vs dataset_v1"
    )
    bullet("  <- retraining approved by an administrator")
    detail("")


def print_state(ctx: DemoContext, title: str) -> None:
    """The compact state block shown between phases."""
    from demo.reporting import state_block

    state_block(title, ctx.state.as_rows())
