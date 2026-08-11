"""RetrainingWorkflow._resolve_candidate_metrics()'s fallback chain —
specifically the branch added so RetrainingWorkflow can drive a real
AirflowOrchestrator.

AirflowOrchestrator.get_execution_status() only ever surfaces DAG-run-level
info (logical_date/conf/...) on ExecutionStatus.metadata — never the
train task's actual metrics/params, unlike LocalDockerOrchestrator, which
replays its subprocess's stdout JSON. Real metrics for an Airflow-driven
run instead arrive via POST /internal/training-runs/{id}/finish (called
by mlops_training_pipeline.py's report_status task), landing under
metadata["orchestrator_result"]["metrics"] — one level deeper than the
top-level "metrics" key the original fallback chain checked.
"""

from __future__ import annotations

import json

from mlops_framework.database.models.training_run import TrainingRun
from mlops_framework.workflow.retraining import RetrainingWorkflow


def _workflow() -> RetrainingWorkflow:
    # _resolve_candidate_metrics touches neither self._session nor
    # self._service in the branches exercised below (only the live
    # orchestrator-requery fallback, which these cases don't reach) — the
    # same __new__() bypass pattern
    # test_dataset_content_verification.py uses for a pure-function method.
    return RetrainingWorkflow.__new__(RetrainingWorkflow)


def _run(metadata: dict) -> TrainingRun:
    # A transient (unpersisted, session-free) instance — fine for a
    # declarative model, and _resolve_candidate_metrics only ever reads
    # these two attributes.
    return TrainingRun(metadata_json=json.dumps(metadata), dataset_version_id=1)


class TestResolveCandidateMetrics:
    def test_top_level_metrics_key_still_wins(self):
        """LocalDockerOrchestrator's path (and anything setting metrics
        directly) keeps taking priority over the nested key."""
        wf = _workflow()
        run = _run(
            {
                "metrics": {"f1": 0.9},
                "orchestrator_result": {"metrics": {"f1": 0.1}},
            }
        )
        assert wf._resolve_candidate_metrics(run, evaluate_model=None) == {"f1": 0.9}

    def test_orchestrator_result_metrics_used_for_an_airflow_driven_run(self):
        """Exactly the shape POST /internal/training-runs/{id}/finish
        leaves behind for a run driven through AirflowOrchestrator: no
        top-level "metrics", only orchestrator_result.metrics."""
        wf = _workflow()
        run = _run(
            {
                "training_entrypoint": "case_studies.fraud_detection.pipelines:train_xgboost",
                "owned_by_workflow": True,
                "orchestrator_result": {
                    "metrics": {"f1": 0.83, "average_precision": 0.71},
                    "params": {"n_estimators": 200},
                    "artifact_path": "s3://bucket/model.json",
                },
            }
        )
        assert wf._resolve_candidate_metrics(run, evaluate_model=None) == {
            "f1": 0.83,
            "average_precision": 0.71,
        }

    def test_orchestrator_result_present_but_metrics_key_missing(self):
        """A malformed/partial report (e.g. train failed before computing
        metrics) must not raise — falls through same as if nothing was
        there at all."""
        wf = _workflow()
        run = _run({"orchestrator_result": {"artifact_path": None}})
        assert wf._resolve_candidate_metrics(run, evaluate_model=None) == {}

    def test_no_metrics_anywhere_is_an_empty_dict_not_an_error(self):
        wf = _workflow()
        run = _run({})
        assert wf._resolve_candidate_metrics(run, evaluate_model=None) == {}
