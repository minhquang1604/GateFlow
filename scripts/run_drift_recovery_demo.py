"""Full demo — MLOps Framework, Fraud Detection case study.

Two phases, one continuous V1 → V2 story:

    PHASE 1 — Initial training, real Airflow DAG
    ─────────────────────────────────────────────
    Dataset -> DatasetVersion v1 (content-hash pinned, verified by the
      Airflow task before it trains on it)
        -> ReadinessEngine -> TrainingEligibilityPolicy
            -> AirflowOrchestrator triggers the real
               ``mlops_training_pipeline`` DAG
                -> train_xgboost (real XGBoost, real MLflow run,
                   content-hash checked)
            -> TrainingRun SUCCESS -> ModelVersion CANDIDATE
                -> ModelPromotionPolicy (cold start) -> PRODUCTION
                    -> real ServingBridge reload event
    This phase is deliberately verbose — dataset profile, readiness
    checks, eligibility reasons, per-task Airflow states, every metric
    — because the point is to leave a rich trail to browse afterwards
    in the Management UI ("Gateflow"): the run detail page, the DAG's
    Graph View + task history, the model's metrics, the lineage graph.

    PHASE 2 — Drift & self-healing
    ─────────────────────────────────────────────
    New DatasetVersion v2 (a covariate shift in *legit* traffic only —
      see case_studies.fraud_detection.data.generate's drift_shift)
        -> DriftDetector (real scipy KS-test) flags it
            -> RetrainingWorkflow.run() — ONE call: readiness -> drift
               -> eligibility (gated on drift having been detected)
               -> training (real XGBoost) -> promotion -> PRODUCTION
        -> V1 scored *live* against the drifted data (collapses) vs
           V2 fresh (recovers) — two different F1s, reported
           separately; see the governance step below for why
        -> Lineage trace back to the exact DatasetVersion and the
           training entrypoint (module:callable) that produced it

Why Phase 1 and Phase 2 use different orchestrators
─────────────────────────────────────────────────────
``RetrainingWorkflow.run()`` forwards ``pipeline_id`` straight to
``TrainingService.create_run()`` with no way to also set
``metadata["training_entrypoint"]`` — the split
``AirflowOrchestrator`` needs (see mlops_training_pipeline.py's
``_resolve_entrypoint``). So Phase 1 (driven directly by this script)
uses ``AirflowOrchestrator`` for the rich Airflow-side detail; Phase 2
(driven by ``RetrainingWorkflow``) uses ``LocalDockerOrchestrator`` — a
real local subprocess, not Docker despite the name (see
orchestration/local.py) — the same pattern already proven in
scripts/run_fraud_detection_e2e.py's real-XGBoost leg. This is a known
framework gap, not a workaround; see the demo write-up's Phụ lục B.

One-time setup before Phase 1 will work
─────────────────────────────────────────
The Airflow image bakes ``case_studies/`` in at BUILD time
(infrastructure/airflow/Dockerfile) — a different image, and a
different filesystem root, than this one (infrastructure/app/Dockerfile).
So ``case_studies/fraud_detection/data/drift_demo_v1.csv`` must already
exist in the repo *before* the Airflow images are (re)built, or the DAG's
``train`` task 404s on a path that isn't there. This script's first
write of that file is deterministic (fixed seed/params) — regenerating
it produces byte-identical content — so once committed, rebuilding is
only needed again if the generator itself changes.

Usage::

    # one-time, whenever drift_demo_v1.csv is new/changed:
    docker compose --env-file .env.docker build airflow-webserver airflow-scheduler
    docker compose --env-file .env.docker up -d

    MLFLOW_TRACKING_URI=http://localhost:5000 \\
    MLFLOW_S3_ENDPOINT_URL=http://localhost:9000 \\
    AWS_ACCESS_KEY_ID=minioadmin AWS_SECRET_ACCESS_KEY=minioadmin \\
    AIRFLOW_BASE_URL=http://localhost:8080 \\
    python -m scripts.run_drift_recovery_demo

Or, from inside the docker network (e.g. ``docker compose run --rm
app python -m scripts.run_drift_recovery_demo``), the MinIO/Airflow
env vars are already set on the ``app``/``serving``/``demo`` services —
see docker-compose.yml.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

import httpx
from sqlalchemy import select

from mlops_framework.config.settings import get_settings
from mlops_framework.database.base import Base
from mlops_framework.database.models.dataset_version import DatasetVersion
from mlops_framework.database.models.model import Model as ModelRow
from mlops_framework.database.models.model_version import ModelState, ModelVersion
from mlops_framework.database.session import DatabaseManager
from mlops_framework.dataset.checksum import calculate_file_checksum
from mlops_framework.dataset.manager import DatasetManager
from mlops_framework.drift.detector import (
    DriftConfig,
    DriftService,
    ScipyDriftDetector,
)
from mlops_framework.governance.eligibility import (
    EligibilityConfig,
    TrainingEligibilityPolicy,
)
from mlops_framework.governance.promotion import PromotionConfig
from mlops_framework.lineage.manager import LineageManager
from mlops_framework.model.manager import ModelManager
from mlops_framework.orchestration.airflow import AirflowOrchestrator
from mlops_framework.orchestration.local import LocalDockerOrchestrator
from mlops_framework.readiness.engine import ReadinessEngine, TrainingPolicy
from mlops_framework.tracking.mlflow import MLflowTracker
from mlops_framework.training.manager import TrainingManager
from mlops_framework.training.service import TrainingService
from mlops_framework.workflow.retraining import RetrainingWorkflow

from case_studies.fraud_detection import data as fraud_data

# --------------------------------------------------------------------- #
# Constants — calibrated so V1 self-evaluated F1 ≈ 0.92, V1 scored on
# drifted data collapses to ≈ 0.46-0.50, V2 retrained on the drift
# recovers to ≈ 0.80-0.85. See the demo write-up's Phụ lục A for the
# calibration sweep.
# --------------------------------------------------------------------- #
DATASET_NAME = "credit-card-fraud-drift-demo"
MODEL_NAME = "fraud-xgboost-drift-demo"
PIPELINE_FRIENDLY = "fraud-xgboost-drift-demo"
PIPELINE_ID = "case_studies.fraud_detection.pipelines:train_xgboost"
# AirflowOrchestrator.trigger_pipeline() takes the DAG id, not the
# module:callable path — the callable travels separately in
# metadata["training_entrypoint"] (see PIPELINE_ID above).
DAG_ID = os.environ.get("AIRFLOW_DAG_ID", "mlops_training_pipeline")
N_ROWS = 8000
FRAUD_RATIO = 0.02
SEED = 42
DRIFT_SHIFT = 1.0

DATA_DIR = REPO_ROOT / "case_studies" / "fraud_detection" / "data"
V1_CSV_LOCAL = DATA_DIR / "drift_demo_v1.csv"
V2_CSV_LOCAL = DATA_DIR / "drift_demo_v2.csv"
# Valid inside the Airflow containers only (see module docstring) — NOT
# a path this script itself can open.
AIRFLOW_V1_CSV_PATH = "/opt/case_studies/fraud_detection/data/drift_demo_v1.csv"

STEP_SEP = "─" * 72


def _print_banner(title: str) -> None:
    print(f"\n{STEP_SEP}\n  {title}\n{STEP_SEP}")


def _detail(message: str) -> None:
    print(f"  • {message}", flush=True)


def _wait_for(url: str, *, label: str, timeout: float = 180.0) -> None:
    """Poll ``url`` until it returns 2xx/3xx or we time out."""
    print(f"  waiting for {label} at {url} ...", flush=True)
    deadline = time.time() + timeout
    last_err: Optional[str] = None
    while time.time() < deadline:
        try:
            response = httpx.get(url, timeout=2.0)
            if response.status_code < 500:
                print(f"  ✓ {label} is up ({response.status_code})")
                return
        except Exception as exc:
            last_err = repr(exc)
        time.sleep(2.0)
    raise RuntimeError(
        f"{label} at {url} did not become ready within {timeout:.0f}s "
        f"(last error: {last_err})"
    )


def _resolve_endpoints(settings) -> dict[str, str]:
    return {
        "mlflow_uri": os.environ.get("MLFLOW_TRACKING_URI") or settings.mlflow_tracking_uri or "http://localhost:5000",
        "airflow_url": os.environ.get("AIRFLOW_BASE_URL") or settings.airflow_base_url or "http://localhost:8080",
        "serving_url": os.environ.get("SERVING_BRIDGE_URL") or settings.serving_bridge_url or "http://localhost:8001",
        # The Airflow `train` task always executes inside the
        # airflow-scheduler container (LocalExecutor), on the docker
        # network — so the tracking_uri handed to *it* (via TrainingRun
        # metadata) must be the in-network service name, regardless of
        # what URL *this* process used above to reach MLflow (localhost
        # when run from the host, `mlflow` when run inside the network).
        # Conflating the two makes the DAG's train task try to reach
        # "localhost:5000" from inside its own container and silently
        # skip all MLflow logging (params, metrics, artifact).
        "mlflow_uri_for_airflow": os.environ.get("AIRFLOW_INTERNAL_MLFLOW_URI", "http://mlflow:5000"),
    }


def _training_policy() -> TrainingPolicy:
    return TrainingPolicy(
        required_size=1000,
        freshness_hours=24 * 365,
        required_columns=fraud_data.feature_columns() + [fraud_data.target_column()],
        expected_column_count=31,
        max_missing_ratio=0.0,
    )


def _feature_frame(csv_path: Path) -> dict[str, list[float]]:
    """Load a fraud CSV into the ``{feature: [values]}`` shape the drift
    detector expects — every feature column, not just the shifted ones,
    so the KS test also confirms the *unshifted* features do NOT flag."""
    import pandas as pd

    df = fraud_data.normalize_columns(pd.read_csv(csv_path))
    return {col: df[col].astype(float).tolist() for col in fraud_data.feature_columns()}


# ======================================================================= #
# PHASE 1 — Initial training, real Airflow DAG
# ======================================================================= #


def phase1_initial_training(db: DatabaseManager, endpoints: dict[str, str], settings, timeout: float):
    """Train + promote V1 through the real Airflow DAG. Returns
    (v1_version, model_row, v1_model_version)."""

    _print_banner("PHASE 1 · 1/6  Dataset + DatasetVersion (content-hash pinned)")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fraud_data.write_csv(V1_CSV_LOCAL, n_rows=N_ROWS, fraud_ratio=FRAUD_RATIO, seed=SEED, drift_shift=0.0)
    profile = fraud_data.describe_csv(V1_CSV_LOCAL)
    content_sha256 = calculate_file_checksum(V1_CSV_LOCAL)
    metadata = dict(profile["metadata"])
    metadata["content_sha256"] = content_sha256
    metadata["size_bytes"] = V1_CSV_LOCAL.stat().st_size
    _detail(
        f"wrote {V1_CSV_LOCAL.name} — {profile['row_count']:,} rows, "
        f"{metadata['n_fraud']} fraud ({metadata['fraud_ratio']:.4%})"
    )
    _detail(f"content_sha256={content_sha256[:16]}… — the Airflow task verifies this before training")

    with db.get_session() as session:
        dm = DatasetManager(session)
        dataset = dm.get_dataset_by_name(DATASET_NAME)
        if dataset is None:
            dataset = dm.create_dataset(
                DATASET_NAME, description="Fraud detection — drift & self-healing demo"
            )
            _detail(f"created dataset {dataset.name!r} (id={dataset.id})")
        else:
            _detail(f"dataset {dataset.name!r} already exists (id={dataset.id}) — reusing")

        existing = [
            v for v in dm.list_versions(dataset.id)
            if json.loads(v.metadata_json or "{}").get("content_sha256") == content_sha256
        ]
        if existing:
            version = existing[-1]
            _detail(f"identical content already registered as v{version.version_number}")
        else:
            version = dm.create_version(
                dataset_id=dataset.id,
                storage_uri=AIRFLOW_V1_CSV_PATH,
                row_count=profile["row_count"],
                metadata=metadata,
            )
            _detail(
                f"registered DatasetVersion v{version.version_number} (id={version.id}) "
                f"— storage_uri (Airflow-side) = {AIRFLOW_V1_CSV_PATH}"
            )
        session.commit()
        dataset_id, version_id = dataset.id, version.id

    _print_banner("PHASE 1 · 2/6  ReadinessEngine")
    with db.get_session() as session:
        version_row = session.get(DatasetVersion, version_id)
        readiness = ReadinessEngine(session).evaluate(version_row, policy=_training_policy())
        for name, outcome in readiness.check_dict().items():
            _detail(f"{name:20} {outcome}")
        _detail(f"=> {readiness.status.value}")
        if not readiness.is_ready:
            raise SystemExit(f"dataset BLOCKED: {readiness.reasons}")

    _print_banner("PHASE 1 · 3/6  Model + TrainingEligibilityPolicy")
    with db.get_session() as session:
        mm = ModelManager(session)
        model_row = mm.get_model_by_name(MODEL_NAME)
        if model_row is None:
            model_row = mm.create_model(
                MODEL_NAME,
                task="binary_classification",
                description="XGBoost fraud classifier — drift & self-healing demo",
            )
            _detail(f"created model {MODEL_NAME!r} (id={model_row.id})")
        else:
            _detail(f"model {MODEL_NAME!r} already exists (id={model_row.id}) — reusing")
        session.commit()
        model_id = model_row.id

    with db.get_session() as session:
        version_row = session.get(DatasetVersion, version_id)
        model_row = session.get(ModelRow, model_id)
        ctx = TrainingEligibilityPolicy(session).build_context(
            dataset_version=version_row, readiness=readiness, model=model_row, force=True,
        )
        decision = TrainingEligibilityPolicy(session).evaluate(ctx, EligibilityConfig())
        _detail(f"eligible={decision.eligible} reasons={decision.reasons}")

    _print_banner("PHASE 1 · 4/6  Training — real Airflow DAG → train_xgboost")
    tracker = MLflowTracker(tracking_uri=endpoints["mlflow_uri"], experiment_name="fraud-drift-demo")
    with AirflowOrchestrator(
        base_url=endpoints["airflow_url"],
        username=settings.airflow_username or "airflow",
        password=settings.airflow_password or "airflow",
    ) as orchestrator:
        with db.get_session() as session:
            dm = DatasetManager(session)
            tm = TrainingManager(session, dm)
            service = TrainingService(training_manager=tm, orchestrator=orchestrator, tracker=tracker)
            run = service.create_run(
                dataset_version_id=version_id,
                pipeline_id=DAG_ID,
                trigger_type="MANUAL",
                metadata={
                    "training_entrypoint": PIPELINE_ID,
                    "parameters": {"max_depth": 6, "n_estimators": 200, "learning_rate": 0.1},
                    "csv_uri": AIRFLOW_V1_CSV_PATH,
                    "model_name": MODEL_NAME,
                    "min_f1": 0.5,
                    "tracking_uri": endpoints["mlflow_uri_for_airflow"],
                },
            )
            run_id = run.id
            _detail(f"TrainingRun created (id={run_id})")

            tracker_run_id = tracker.start_run(
                run_name=f"training-run-{run_id}",
                tags={"training_run_id": str(run_id), "pipeline_id": PIPELINE_ID},
            )
            tracker.log_params(
                {"pipeline": PIPELINE_FRIENDLY, "csv_uri": AIRFLOW_V1_CSV_PATH, "n_rows": N_ROWS, "fraud_ratio": FRAUD_RATIO}
            )
            _detail(f"MLflow run started (id={tracker_run_id})")
            tm.update_metadata(
                run_id,
                {
                    "tracker_run_id": tracker_run_id,
                    "tracking_uri": endpoints["mlflow_uri_for_airflow"],
                    "orchestrator_execution_id": "pending",
                },
            )
            execution_id = service.start_run(run_id)
            tm.update_metadata(run_id, {"orchestrator_execution_id": execution_id})
            _detail(f"triggered Airflow DAG run: {execution_id}")

        _print_banner("PHASE 1 · 5/6  Waiting for the Airflow DAG run to finish")
        deadline = time.time() + timeout
        final_state = "UNKNOWN"
        while time.time() < deadline:
            status = orchestrator.get_execution_status(execution_id)
            tasks = orchestrator.get_task_instances(execution_id)
            task_summary = (
                ", ".join(f"{t['task_id']}={t['state']}" for t in tasks) if tasks else "(scheduling…)"
            )
            _detail(f"{status.state.value:<10} {task_summary}")
            if status.state.value in ("SUCCESS", "FAILED", "CANCELLED"):
                final_state = status.state.value
                break
            time.sleep(3.0)

        if final_state != "SUCCESS":
            tracker.end_run(status="FAILED")
            raise SystemExit(f"Airflow DAG run did not succeed (final={final_state})")

        # The DAG's own `report_status` task already calls the app's
        # internal API (POST .../finish) to complete the run — only
        # complete it here if that somehow didn't happen.
        with db.get_session() as session:
            dm = DatasetManager(session)
            tm = TrainingManager(session, dm)
            current_status = tm.get_run(run_id).status.value
            if current_status not in ("SUCCESS", "FAILED", "CANCELLED"):
                TrainingService(training_manager=tm, orchestrator=orchestrator, tracker=tracker).complete_run(run_id)
                current_status = tm.get_run(run_id).status.value
        tracker.end_run(status="SUCCESS")
        _detail(f"TrainingRun {run_id} status={current_status}")

    # The DAG's `register_and_promote` task already applied
    # ModelPromotionPolicy server-side and, on approval, published the
    # ServingBridge reload — this step only reads back what it decided.
    _print_banner("PHASE 1 · 6/6  Model evaluation + promotion (decided by the DAG) + serving reload")
    with db.get_session() as session:
        latest_mv = session.execute(
            select(ModelVersion)
            .where(ModelVersion.model_id == model_id)
            .order_by(ModelVersion.version_number.desc())
            .limit(1)
        ).scalars().first()
        if latest_mv is None:
            raise SystemExit("no ModelVersion found after training")
        metrics = json.loads(latest_mv.metrics_json or "{}")
        for k, v in metrics.items():
            _detail(f"{k:20} {v:.4f}" if isinstance(v, float) else f"{k}: {v}")
        _detail(f"ModelVersion v{latest_mv.version_number} state={latest_mv.state.value}")
        if latest_mv.state != ModelState.PRODUCTION:
            raise SystemExit(
                f"model not in PRODUCTION (state={latest_mv.state.value}) — cannot continue to Phase 2"
            )
        # Detach the values Phase 2 needs before the session closes.
        v1_mv_id, v1_mv_mlflow_run_id = latest_mv.id, latest_mv.mlflow_run_id

    resp = httpx.get(f"{endpoints['serving_url']}/internal/model/active/{MODEL_NAME}", timeout=5.0)
    if resp.status_code == 200:
        _detail(f"serving bridge reports active model_version={resp.json().get('model_version_number')}")
    else:
        _detail(f"! serving bridge returned {resp.status_code}: {resp.text}")

    _detail(f"MLflow run id: {tracker_run_id} — search it under experiment 'fraud-drift-demo' at {endpoints['mlflow_uri']}")
    _detail(f"Airflow run:   {endpoints['airflow_url']}/dags/{DAG_ID}/grid?dag_run_id={execution_id.split('/', 1)[-1]}")

    return dataset_id, version_id, model_id, v1_mv_id, v1_mv_mlflow_run_id


# ======================================================================= #
# PHASE 2 — Drift & self-healing
# ======================================================================= #


def phase2_inject_drift(db: DatabaseManager, dataset_id: int) -> int:
    """Register a covariate-shifted DatasetVersion v2, run a real
    KS-test against V1. Returns the new DatasetVersion's id."""
    _print_banner("PHASE 2 · 1/4  Drift injection — new DatasetVersion, real KS-test")
    fraud_data.write_csv(V2_CSV_LOCAL, n_rows=N_ROWS, fraud_ratio=FRAUD_RATIO, seed=SEED, drift_shift=DRIFT_SHIFT)
    _detail(f"wrote {V2_CSV_LOCAL.name} — same population as V1, drift_shift={DRIFT_SHIFT}")

    with db.get_session() as session:
        dm = DatasetManager(session)
        existing = [v for v in dm.list_versions(dataset_id) if v.storage_uri == str(V2_CSV_LOCAL)]
        if existing:
            version = existing[-1]
            _detail(f"DatasetVersion v{version.version_number} already registered — reusing")
        else:
            version = dm.create_version(
                dataset_id=dataset_id,
                storage_uri=str(V2_CSV_LOCAL),
                row_count=N_ROWS,
                metadata=fraud_data.schema_metadata(),
            )
            _detail(f"registered DatasetVersion v{version.version_number} (id={version.id})")
        session.commit()
        version_id = version.id

        version_row = session.get(DatasetVersion, version_id)
        readiness = ReadinessEngine(session).evaluate(version_row, policy=_training_policy())
        _detail(f"readiness => {readiness.status.value} (schema unchanged — only statistics drifted)")

        v1_version = next(
            v for v in dm.list_versions(dataset_id) if v.storage_uri == AIRFLOW_V1_CSV_PATH
        )
        ref_data = _feature_frame(V1_CSV_LOCAL)
        cur_data = _feature_frame(V2_CSV_LOCAL)
        drift_service = DriftService(session, ScipyDriftDetector())
        result = drift_service.evaluate(
            reference_version=v1_version,
            current_version=version_row,
            reference_data=ref_data,
            current_data=cur_data,
            config=DriftConfig(threshold=0.05),
            notes="Phase 2 — drift injection",
        )
        session.commit()

    drifted = [fr.feature for fr in result.feature_results if fr.drift_detected]
    stable = [fr.feature for fr in result.feature_results if not fr.drift_detected]
    _detail(f"=> drift_detected={result.drift_detected} (score={result.score:.4f})")
    _detail(f"   drifted features : {', '.join(drifted) or '(none)'}")
    _detail(f"   stable features  : {len(stable)} feature(s) unaffected — confirms a targeted shift")
    return version_id


def phase2_auto_retrain(db: DatabaseManager, version_id: int, model_id: int, mlflow_uri: str) -> Any:
    """Drive RetrainingWorkflow.run() — it hardcodes a 60s training wait,
    ample for LocalDockerOrchestrator on this dataset size."""
    _print_banner("PHASE 2 · 2/4  Auto-react — RetrainingWorkflow.run() end-to-end")
    with db.get_session() as session:
        dm = DatasetManager(session)
        version_row = session.get(DatasetVersion, version_id)
        model_row = session.get(ModelRow, model_id)
        ref_data = _feature_frame(V1_CSV_LOCAL)
        cur_data = _feature_frame(V2_CSV_LOCAL)

        tm = TrainingManager(session, dm)
        tracker = MLflowTracker(tracking_uri=mlflow_uri, experiment_name="fraud-drift-demo")
        orchestrator = LocalDockerOrchestrator()
        service = TrainingService(training_manager=tm, orchestrator=orchestrator, tracker=tracker)
        drift_service = DriftService(session, ScipyDriftDetector())

        workflow = RetrainingWorkflow(session, training_service=service, drift_service=drift_service)
        try:
            outcome = workflow.run(
                dataset_version=version_row,
                model=model_row,
                training_policy=_training_policy(),
                eligibility_config=EligibilityConfig(require_drift_to_retrain=True),
                # Deliberate: gate on an absolute quality floor, not
                # must_beat_production — see the module docstring's
                # Phase 2 note on why V1's *stored* metric stops being a
                # fair bar once drift has happened.
                promotion_config=PromotionConfig(
                    min_metrics={"f1": 0.70, "precision": 0.70},
                    must_beat_production=False,
                    allow_cold_start=True,
                ),
                reference_data=ref_data,
                current_data=cur_data,
                pipeline_id=PIPELINE_ID,
                force=False,
            )
        finally:
            orchestrator.shutdown()
        session.commit()

    for step in outcome.steps:
        _detail(f"[{step.name:12}] {'OK' if step.passed else 'BLOCKED'} — {step.detail}")
    _detail(f"=> promoted={outcome.promoted} blocked_reason={outcome.blocked_reason}")
    return outcome


def phase2_governance(db: DatabaseManager, v1_mv_id: int, v1_mlflow_run_id: Optional[str], outcome: Any) -> None:
    """Two different F1s, reported separately: V1 scored live on the
    drifted data (the "V1 collapsed" story) vs the number
    ModelPromotionPolicy actually compared (V2-fresh vs V1-stored)."""
    _print_banner("PHASE 2 · 3/4  Governance — V1-live vs V2-fresh vs V1-stored")

    v1_live: dict[str, float] = {}
    if v1_mlflow_run_id:
        import mlflow
        import numpy as np
        import pandas as pd
        import xgboost as xgb
        from sklearn.metrics import f1_score, precision_score, recall_score

        local_path = mlflow.artifacts.download_artifacts(run_id=v1_mlflow_run_id, artifact_path="model.json")
        model = xgb.XGBClassifier()
        model.load_model(local_path)
        df = fraud_data.normalize_columns(pd.read_csv(V2_CSV_LOCAL))
        X = df[fraud_data.feature_columns()].to_numpy(dtype=np.float32)
        y = df[fraud_data.target_column()].to_numpy(dtype=np.int32)
        pred = model.predict(X)
        v1_live = {
            "f1": float(f1_score(y, pred, zero_division=0)),
            "precision": float(precision_score(y, pred, zero_division=0)),
            "recall": float(recall_score(y, pred, zero_division=0)),
        }
        _detail(
            f"V1 (production) scored LIVE on drifted data: "
            f"F1={v1_live['f1']:.4f} precision={v1_live['precision']:.4f} recall={v1_live['recall']:.4f}"
        )
    else:
        _detail("V1 has no mlflow_run_id — skipping live re-score")

    with db.get_session() as session:
        v1_mv = session.get(ModelVersion, v1_mv_id)
        v1_stored = json.loads(v1_mv.metrics_json or "{}") if v1_mv else {}
        if v1_stored.get("f1") is not None:
            _detail(f"   ...vs V1's stored training-time F1={v1_stored['f1']:.4f}")

        v2_mv = None
        if outcome.model_version_id:
            v2_mv = ModelManager(session).get_model_version(outcome.model_version_id)
        if v2_mv is not None:
            v2_metrics = json.loads(v2_mv.metrics_json or "{}")
            _detail(f"V2 (candidate) trained fresh on drifted data: F1={v2_metrics.get('f1', 0):.4f}")
            recovered = bool(v1_live) and v2_metrics.get("f1", 0) > v1_live["f1"]
            _detail(
                f"=> V2 recovers {'well above' if recovered else 'toward'} "
                f"V1's collapsed live score; state={v2_mv.state.value}"
            )


def phase2_lineage(db: DatabaseManager, outcome: Any, fallback_model_version_id: int) -> None:
    _print_banner("PHASE 2 · 4/4  Lineage — trace ModelVersion back to Dataset + source code")
    target_id = outcome.model_version_id or fallback_model_version_id
    with db.get_session() as session:
        graph = LineageManager(session).graph_for_model_version(target_id)
        for node in graph.nodes:
            _detail(f"{node.type:16} {node.label}  {node.attributes or {}}")
        _detail(f"{len(graph.edges)} edges")


# ======================================================================= #
# Entry point
# ======================================================================= #


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-wait", action="store_true", help="skip the initial service-health wait")
    parser.add_argument("--timeout", type=float, default=600.0, help="Airflow DAG run timeout, seconds")
    args = parser.parse_args()

    get_settings.cache_clear()
    settings = get_settings()
    endpoints = _resolve_endpoints(settings)

    print("MLOps Framework — Full Demo: Initial Training + Drift & Self-Healing")
    print(STEP_SEP)
    print(f"  DATABASE_URL     = {settings.database_url}")
    print(f"  MLFLOW_TRACKING  = {endpoints['mlflow_uri']}")
    print(f"  AIRFLOW_BASE_URL = {endpoints['airflow_url']}")
    print(f"  SERVING_BRIDGE   = {endpoints['serving_url']}")

    if not args.skip_wait:
        _wait_for(f"{endpoints['airflow_url']}/health", label="Airflow")
        _wait_for(f"{endpoints['mlflow_uri']}/health", label="MLflow")
        _wait_for(f"{endpoints['serving_url']}/healthz", label="ServingBridge")

    db = DatabaseManager(settings.database_url)
    Base.metadata.create_all(db.engine)

    dataset_id, v1_version_id, model_id, v1_mv_id, v1_mlflow_run_id = phase1_initial_training(
        db, endpoints, settings, timeout=args.timeout
    )

    v2_version_id = phase2_inject_drift(db, dataset_id)
    outcome = phase2_auto_retrain(db, v2_version_id, model_id, endpoints["mlflow_uri"])
    phase2_governance(db, v1_mv_id, v1_mlflow_run_id, outcome)
    phase2_lineage(db, outcome, fallback_model_version_id=v1_mv_id)

    _print_banner("✓ Demo complete")
    print("Inspect the results:")
    print("  • Management UI  (Gateflow)              : http://localhost:8000")
    print(f"  • MLflow UI      (runs, params, metrics)  : {endpoints['mlflow_uri']}")
    print(f"  • Airflow UI     (DAG run, task logs)     : {endpoints['airflow_url']}")
    print("  • MinIO console  (artifacts)              : http://localhost:9001")
    print(
        f"  • ServingBridge  (active model version)   : "
        f"{endpoints['serving_url']}/internal/model/active/{MODEL_NAME}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
