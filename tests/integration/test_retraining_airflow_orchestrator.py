"""RetrainingWorkflow driven by an Airflow-shaped orchestrator.

Every other RetrainingWorkflow test (test_governance_end_to_end.py) uses
LocalDockerOrchestrator, whose ExecutionStatus.metadata carries the
pipeline's actual metrics — AirflowOrchestrator's never does (see
AirflowOrchestrator._to_status(): only DAG-run-level info). Real metrics
for an Airflow-driven run instead arrive out-of-band, the way
infrastructure/airflow/dags/mlops_training_pipeline.py's report_status
task delivers them: a separate POST to
/internal/training-runs/{id}/finish that lands under
metadata["orchestrator_result"]["metrics"] sometime before
wait_for_completion() notices the run reached SUCCESS.

_AirflowShapedOrchestrator below simulates exactly that shape and
ordering without needing a real Airflow — its trigger_pipeline() writes
metrics into the run's metadata the same way report_status's HTTP call
would, and its get_execution_status() reports SUCCESS with metadata that
deliberately has no "metrics" key, so this test fails the moment
RetrainingWorkflow regresses to only knowing the LocalDockerOrchestrator
shape (see workflow/retraining.py's _resolve_candidate_metrics and
training/service.py's wait_for_completion).

The DAG file itself (mlops_training_pipeline.py) can't be imported here
— it requires the ``airflow`` package, which isn't in this project's dev
dependencies (it lives only in infrastructure/airflow's own image). This
test covers everything on the framework side of that HTTP boundary;
the DAG's own owned_by_workflow / skip_lifecycle_transition logic is
verified by running it for real (see the module docstring's
"Real end-to-end demos").
"""

from __future__ import annotations

import json

import pytest

from mlops_framework.database.models.dataset import Dataset
from mlops_framework.database.models.dataset_version import DatasetVersion
from mlops_framework.database.models.model import Model as ModelRow
from mlops_framework.database.models.model_version import ModelState, ModelVersion
from mlops_framework.dataset.manager import DatasetManager
from mlops_framework.events.publisher import InMemoryEventPublisher
from mlops_framework.governance.promotion import PromotionConfig
from mlops_framework.model.manager import ModelManager
from mlops_framework.orchestration.base import (
    ExecutionState,
    ExecutionStatus,
    Orchestrator,
)
from mlops_framework.readiness.engine import TrainingPolicy
from mlops_framework.training.manager import TrainingManager
from mlops_framework.training.service import TrainingService
from mlops_framework.tracking.in_memory import InMemoryTracker
from mlops_framework.workflow.retraining import RetrainingWorkflow

DAG_ID = "mlops_training_pipeline"
TRAINING_ENTRYPOINT = "case_studies.fraud_detection.pipelines:train_xgboost"


class _AirflowShapedOrchestrator(Orchestrator):
    """trigger_pipeline() reports results the way report_status's POST
    /finish does (out-of-band, into TrainingRun.metadata_json);
    get_execution_status() reports SUCCESS with no metrics on it at all
    — matching AirflowOrchestrator._to_status() exactly."""

    def __init__(self, training_manager: TrainingManager, metrics: dict):
        self._tm = training_manager
        self._metrics = metrics
        self.triggered: list[tuple[str, dict]] = []

    def trigger_pipeline(self, pipeline_id, config=None):
        config = dict(config or {})
        self.triggered.append((pipeline_id, config))
        assert pipeline_id == DAG_ID, "pipeline_id must be the dag_id, not the entrypoint"
        run_id = config["training_run_id"]
        # What report_status's POST /internal/training-runs/{id}/finish
        # leaves behind — before this DAG run is ever observed terminal.
        self._tm.update_metadata(
            run_id,
            {
                "orchestrator_result": {
                    "metrics": self._metrics,
                    "params": {"n_estimators": 200},
                    "artifact_path": "s3://bucket/model.json",
                    "pipeline": TRAINING_ENTRYPOINT,
                }
            },
        )
        return f"{DAG_ID}/run-1"

    def get_execution_status(self, execution_id):
        return ExecutionStatus(
            execution_id=execution_id,
            state=ExecutionState.SUCCESS,
            metadata={"logical_date": "2026-08-11T00:00:00Z", "conf": {}},
        )

    def cancel_execution(self, execution_id):
        return ExecutionStatus(execution_id=execution_id, state=ExecutionState.CANCELLED)


@pytest.fixture()
def airflow_wired_workflow(db_session):
    db_session.expire_all()
    tm = TrainingManager(db_session, DatasetManager(db_session))
    orchestrator = _AirflowShapedOrchestrator(
        tm, metrics={"f1": 0.83, "average_precision": 0.71}
    )
    service = TrainingService(
        training_manager=tm,
        orchestrator=orchestrator,
        tracker=InMemoryTracker(),
    )
    events = InMemoryEventPublisher()
    workflow = RetrainingWorkflow(
        session=db_session, training_service=service, event_publisher=events
    )
    return {
        "db_session": db_session,
        "orchestrator": orchestrator,
        "workflow": workflow,
        "events": events,
    }


def _make_dataset_version(session) -> DatasetVersion:
    dm = DatasetManager(session)
    ds = dm.create_dataset(name="fraud-ds", description="d")
    return dm.create_version(
        dataset_id=ds.id,
        storage_uri="s3://b/fraud-ds-v1.csv",
        row_count=5000,
        metadata={"columns": [{"name": "amount", "dtype": "float64"}]},
    )


def _make_model(session) -> ModelRow:
    return ModelManager(session).create_model(name="fraud-model", task="fraud_detection")


class TestRetrainingWorkflowThroughAirflow:
    def test_promotes_using_metrics_reported_out_of_band(self, airflow_wired_workflow):
        db_session = airflow_wired_workflow["db_session"]
        dv = _make_dataset_version(db_session)
        model = _make_model(db_session)

        outcome = airflow_wired_workflow["workflow"].run(
            dataset_version=dv,
            model=model,
            training_policy=TrainingPolicy(required_size=100),
            promotion_config=PromotionConfig(
                min_metrics={"f1": 0.5}, must_beat_production=False, allow_cold_start=True
            ),
            pipeline_id=DAG_ID,
            training_entrypoint=TRAINING_ENTRYPOINT,
            training_timeout=5.0,
        )

        assert outcome.promoted is True, outcome.steps
        db_session.expire_all()
        mv = db_session.get(ModelVersion, outcome.model_version_id)
        assert mv.state == ModelState.PRODUCTION
        assert mv.metrics_json is not None
        assert json.loads(mv.metrics_json) == {"f1": 0.83, "average_precision": 0.71}

    def test_run_metadata_carries_training_entrypoint_and_ownership_flag(
        self, airflow_wired_workflow
    ):
        db_session = airflow_wired_workflow["db_session"]
        dv = _make_dataset_version(db_session)
        model = _make_model(db_session)

        outcome = airflow_wired_workflow["workflow"].run(
            dataset_version=dv,
            model=model,
            training_policy=TrainingPolicy(required_size=100),
            promotion_config=PromotionConfig(
                min_metrics={"f1": 0.5}, must_beat_production=False, allow_cold_start=True
            ),
            pipeline_id=DAG_ID,
            training_entrypoint=TRAINING_ENTRYPOINT,
            training_timeout=5.0,
        )

        db_session.expire_all()
        tm = TrainingManager(db_session)
        meta = tm.get_run_metadata(outcome.training_run_id)
        assert meta["training_entrypoint"] == TRAINING_ENTRYPOINT
        assert meta["owned_by_workflow"] is True

        # trigger_pipeline() got the dag_id, never the entrypoint.
        pipeline_id, _ = airflow_wired_workflow["orchestrator"].triggered[0]
        assert pipeline_id == DAG_ID

    def test_a_second_call_still_only_creates_one_model_version(
        self, airflow_wired_workflow
    ):
        """RetrainingWorkflow itself only ever registers one ModelVersion
        per run — this is what the DAG's owned_by_workflow-gated skip of
        its own register_and_promote task (see the module docstring) is
        protecting against duplicating."""
        db_session = airflow_wired_workflow["db_session"]
        dv = _make_dataset_version(db_session)
        model = _make_model(db_session)

        outcome = airflow_wired_workflow["workflow"].run(
            dataset_version=dv,
            model=model,
            training_policy=TrainingPolicy(required_size=100),
            promotion_config=PromotionConfig(
                min_metrics={"f1": 0.5}, must_beat_production=False, allow_cold_start=True
            ),
            pipeline_id=DAG_ID,
            training_entrypoint=TRAINING_ENTRYPOINT,
            training_timeout=5.0,
        )

        db_session.expire_all()
        mm = ModelManager(db_session)
        versions = mm.list_model_versions(model.id)
        assert len(versions) == 1
        assert versions[0].id == outcome.model_version_id
