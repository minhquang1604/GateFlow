"""Phase 1-2 — Dataset V1 -> Model V1 -> PRODUCTION.

Delegates to :func:`scripts._initial_training.run_initial_training`, the
flow that already drives a real Airflow DAG through readiness ->
eligibility -> training -> promotion. This step exists to give that flow
the closed-loop demo's configuration and to fold its result into the
demo's state — not to reimplement it.

The one thing that differs from the older demo scripts: V1's CSV is
written to the shared ``demo/data`` mount rather than into
``case_studies/``, so it does not have to be committed and baked into the
Airflow image before the DAG can read it. Dataset V2 could never satisfy
that requirement anyway (it is built during the run), so both versions
now travel the same way.
"""

from __future__ import annotations

from demo.context import DemoContext
from demo.reporting import bullet, detail, kv, section
from mlops_framework.database.models.model_version import ModelState
from scripts._initial_training import run_initial_training


def run(ctx: DemoContext) -> None:
    """Train and promote V1. Raises SystemExit if it does not reach
    PRODUCTION — there is no closed loop to demonstrate without a model
    in production to begin with."""
    cfg = ctx.config

    result = run_initial_training(
        ctx.db,
        ctx.endpoints,
        ctx.settings,
        dataset_name=cfg.dataset_name,
        dataset_description="Credit-card fraud — closed-loop MLOps demo",
        model_name=cfg.model_name,
        model_description="XGBoost fraud classifier — closed-loop MLOps demo",
        pipeline_friendly=cfg.model_name,
        pipeline_id=cfg.pipeline_id,
        dag_id=cfg.dag_id,
        experiment_name=cfg.experiment_name,
        csv_local_path=cfg.local_path(cfg.v1_filename),
        airflow_csv_path=cfg.airflow_path(cfg.v1_filename),
        csv_write_kwargs={
            "n_rows": cfg.n_rows,
            "fraud_ratio": cfg.fraud_ratio,
            "seed": cfg.seed,
            # 0.0 — V1 is trained on the reference population by
            # definition. The drift arrives later, as production traffic.
            "drift_shift": 0.0,
        },
        training_params=cfg.training_params,
        min_f1=cfg.v1_min_f1,
        training_policy=cfg.training_policy(),
        timeout=cfg.dag_timeout,
        step_prefix="",
    )

    if not result.promoted:
        raise SystemExit(
            f"Model V1 did not reach PRODUCTION (state={result.model_state}). "
            f"There is no production model to protect, so the rest of the "
            f"demo would be meaningless — stopping here."
        )

    ctx.dataset_id = result.dataset_id
    ctx.v1_version_id = result.dataset_version_id
    ctx.model_id = result.model_id
    ctx.v1_model_version_id = result.model_version_id
    ctx.v1_mlflow_run_id = result.mlflow_run_id

    ctx.state.dataset_version = f"dataset_v{_version_number(ctx, result.dataset_version_id)}"
    ctx.state.dataset_version_id = result.dataset_version_id
    ctx.state.model_version = f"model_v{result.model_version_number}"
    ctx.state.model_version_id = result.model_version_id
    ctx.state.model_state = ModelState.PRODUCTION.value

    section("Model V1 is now the production model")
    kv("Dataset version", f"id={result.dataset_version_id}", width=20)
    kv("Content sha256", f"{result.content_sha256[:16]}...", width=20)
    kv("Training run", result.execution_id, width=20)
    kv("MLflow run", result.mlflow_run_id or "(none)", width=20)
    kv("Model version", f"v{result.model_version_number}", width=20)
    detail("")
    detail("Traceability established:")
    bullet(f"model_v{result.model_version_number} trained_from dataset "
           f"version #{result.dataset_version_id}")
    bullet(f"dataset version #{result.dataset_version_id} pinned at "
           f"content_sha256={result.content_sha256[:16]}...")

    ctx.record(
        "initial-training",
        "MODEL_PROMOTED",
        status="PRODUCTION",
        training_run=result.execution_id,
        mlflow_run_id=result.mlflow_run_id,
        metrics=_round(result.metrics),
    )


def _version_number(ctx: DemoContext, dataset_version_id: int) -> int:
    from mlops_framework.database.models.dataset_version import DatasetVersion

    with ctx.db.get_session() as session:
        row = session.get(DatasetVersion, dataset_version_id)
        return row.version_number if row is not None else 1


def _round(metrics: dict) -> dict:
    out = {}
    for key, value in (metrics or {}).items():
        try:
            out[key] = round(float(value), 4)
        except (TypeError, ValueError):
            out[key] = value
    return out
