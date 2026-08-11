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
    It is the same flow ``run_end_to_end_demo.py`` runs on its own —
    see :mod:`scripts._initial_training`, which both scripts call.

    PHASE 2 — Drift & self-healing
    ─────────────────────────────────────────────
    New DatasetVersion v2 (a covariate shift in *legit* traffic only —
      see case_studies.fraud_detection.data.generate's drift_shift)
        -> DriftDetector (real scipy KS-test) flags it
            -> Telegram admin-approval gate — the workflow blocks here
               and waits for a real Approve/Deny button press before
               spending any compute on a retrain (see
               scripts/_telegram_approval.py). Deny or timeout stops
               the demo here, on purpose.
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
Not a framework gap anymore — ``RetrainingWorkflow.run()`` takes a
``training_entrypoint`` parameter now, and works with
``AirflowOrchestrator`` (see the README's "Automated retraining
workflow" section). Phase 2 still uses ``LocalDockerOrchestrator`` — a
real local subprocess, not Docker despite the name (see
orchestration/local.py) — for a narrower, script-specific reason:
``phase2_inject_drift`` registers V2's ``DatasetVersion`` with
``storage_uri=str(V2_CSV_LOCAL)``, a path on the host this script runs
on. That's correct for a local subprocess reading it directly, but not
a path that exists inside the Airflow containers, which bake
``case_studies/`` in at build time under ``/opt/case_studies/...`` (see
"One-time setup" below) — training V2 through Airflow would need a
second baked-in path the way V1 already has one
(``AIRFLOW_V1_CSV_PATH``), plus a rebuild. Out of scope for this demo
script; see ``scripts/run_fraud_detection_e2e.py``'s real-XGBoost leg
for the same ``LocalDockerOrchestrator`` pattern used deliberately,
not as a workaround.

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
    TELEGRAM_BOT_TOKEN=... TELEGRAM_ADMIN_CHAT_ID=... \\
    python -m scripts.run_drift_recovery_demo

Or, from inside the docker network (e.g. ``docker compose run --rm
app python -m scripts.run_drift_recovery_demo``), the MinIO/Airflow
env vars are already set on the ``app``/``serving``/``demo`` services —
see docker-compose.yml. ``TELEGRAM_BOT_TOKEN`` / ``TELEGRAM_ADMIN_CHAT_ID``
come from ``.env`` either way — see .env.example.

Pass ``--auto-approve`` to skip the Telegram wait entirely (CI / offline
runs where nobody is watching a phone) — not the demo's default path.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from mlops_framework.config.settings import get_settings
from mlops_framework.database.base import Base
from mlops_framework.database.models.dataset_version import DatasetVersion
from mlops_framework.database.models.model import Model as ModelRow
from mlops_framework.database.models.model_version import ModelVersion
from mlops_framework.database.session import DatabaseManager
from mlops_framework.dataset.manager import DatasetManager
from mlops_framework.drift.detector import (
    DriftConfig,
    DriftService,
    ScipyDriftDetector,
)
from mlops_framework.governance.eligibility import EligibilityConfig
from mlops_framework.governance.promotion import PromotionConfig
from mlops_framework.lineage.manager import LineageManager
from mlops_framework.model.manager import ModelManager
from mlops_framework.orchestration.local import LocalDockerOrchestrator
from mlops_framework.readiness.engine import ReadinessEngine
from mlops_framework.tracking.mlflow import MLflowTracker
from mlops_framework.training.manager import TrainingManager
from mlops_framework.training.service import TrainingService
from mlops_framework.workflow.retraining import RetrainingWorkflow

from case_studies.fraud_detection import data as fraud_data

from scripts._initial_training import (
    STEP_SEP,
    _detail,
    _print_banner,
    _wait_for,
    default_training_policy,
    resolve_endpoints,
    run_initial_training,
)
from scripts._telegram_approval import TelegramApprovalGate

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
    """Train + promote V1 through the real Airflow DAG — delegates to
    the flow shared with ``run_end_to_end_demo.py``. Returns
    (dataset_id, v1_version_id, model_id, v1_mv_id, v1_mlflow_run_id)."""
    result = run_initial_training(
        db,
        endpoints,
        settings,
        dataset_name=DATASET_NAME,
        dataset_description="Fraud detection — drift & self-healing demo",
        model_name=MODEL_NAME,
        model_description="XGBoost fraud classifier — drift & self-healing demo",
        pipeline_friendly=PIPELINE_FRIENDLY,
        pipeline_id=PIPELINE_ID,
        dag_id=DAG_ID,
        experiment_name="fraud-drift-demo",
        csv_local_path=V1_CSV_LOCAL,
        airflow_csv_path=AIRFLOW_V1_CSV_PATH,
        csv_write_kwargs={"n_rows": N_ROWS, "fraud_ratio": FRAUD_RATIO, "seed": SEED, "drift_shift": 0.0},
        training_params={"max_depth": 6, "n_estimators": 200, "learning_rate": 0.1},
        min_f1=0.5,
        training_policy=default_training_policy(),
        timeout=timeout,
        step_prefix="PHASE 1 ·",
    )
    if not result.promoted:
        raise SystemExit(
            f"model not in PRODUCTION (state={result.model_state}) — cannot continue to Phase 2"
        )
    return (
        result.dataset_id,
        result.dataset_version_id,
        result.model_id,
        result.model_version_id,
        result.mlflow_run_id,
    )


# ======================================================================= #
# PHASE 2 — Drift & self-healing
# ======================================================================= #


def phase2_inject_drift(db: DatabaseManager, dataset_id: int) -> int:
    """Register a covariate-shifted DatasetVersion v2, run a real
    KS-test against V1. Returns the new DatasetVersion's id."""
    _print_banner("PHASE 2 · 1/5  Drift injection — new DatasetVersion, real KS-test")
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
        readiness = ReadinessEngine(session).evaluate(version_row, policy=default_training_policy())
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
    return version_id, result.drift_detected, result.score, drifted


def phase2_request_approval(
    settings: Any,
    *,
    drift_detected: bool,
    drift_score: float,
    drifted_features: list[str],
    auto_approve: bool,
) -> bool:
    """Block on a real Telegram Approve/Deny before Phase 2 spends any
    compute on a retrain. Returns True iff the retrain should proceed."""
    _print_banner("PHASE 2 · 2/5  Admin approval gate (Telegram) — waiting before retraining")

    if auto_approve:
        _detail("--auto-approve set — skipping the Telegram wait, proceeding as approved")
        return True

    summary = (
        f"🔔 *Drift detected* on `{DATASET_NAME}`\n"
        f"Model: `{MODEL_NAME}`\n"
        f"Drift score: `{drift_score:.4f}` (threshold 0.05)\n"
        f"Drifted features: {', '.join(drifted_features) or '(none)'}\n\n"
        f"Retrain on the drifted data now?"
    )
    gate = TelegramApprovalGate.from_settings(settings)
    result = gate.request_approval(summary, timeout=settings.telegram_approval_timeout_seconds)
    _detail(f"admin decision: approved={result.approved} ({result.reason})")
    return result.approved


def phase2_auto_retrain(db: DatabaseManager, version_id: int, model_id: int, mlflow_uri: str) -> Any:
    """Drive RetrainingWorkflow.run() — it hardcodes a 60s training wait,
    ample for LocalDockerOrchestrator on this dataset size."""
    _print_banner("PHASE 2 · 3/5  Auto-react — RetrainingWorkflow.run() end-to-end")
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
                training_policy=default_training_policy(),
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
    _print_banner("PHASE 2 · 4/5  Governance — V1-live vs V2-fresh vs V1-stored")

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
    _print_banner("PHASE 2 · 5/5  Lineage — trace ModelVersion back to Dataset + source code")
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
    parser.add_argument(
        "--auto-approve",
        action="store_true",
        help="Skip the Telegram approval gate and retrain immediately (CI/offline use only).",
    )
    args = parser.parse_args()

    get_settings.cache_clear()
    settings = get_settings()
    endpoints = resolve_endpoints(settings)

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

    v2_version_id, drift_detected, drift_score, drifted_features = phase2_inject_drift(db, dataset_id)

    approved = phase2_request_approval(
        settings,
        drift_detected=drift_detected,
        drift_score=drift_score,
        drifted_features=drifted_features,
        auto_approve=args.auto_approve,
    )
    if not approved:
        _print_banner("✗ Retrain blocked — admin denied or did not respond in time")
        print("  • Management UI  (Gateflow)   : http://localhost:8000")
        print("  • V1 remains in PRODUCTION; no retrain was attempted.")
        return 4

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
