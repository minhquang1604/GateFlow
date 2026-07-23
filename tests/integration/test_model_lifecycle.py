"""Integration tests for Model + ModelVersion lifecycle."""

import pytest

from mlops_framework.dataset.manager import DatasetManager
from mlops_framework.exceptions import (
    DuplicateModelNameError,
    InvalidModelStateTransitionError,
    ModelNotFoundError,
    ModelVersionNotFoundError,
)
from mlops_framework.model.manager import ModelManager
from mlops_framework.database.models.model_version import ModelState


def _setup_dataset_version(db_session):
    dm = DatasetManager(db_session)
    ds = dm.create_dataset(name="ds", description="d")
    version = dm.create_version(
        dataset_id=ds.id, storage_uri="s3://b/v1.csv", row_count=100
    )
    return dm, ds, version


class TestModelCRUD:
    def test_create_and_get_model(self, db_session):
        mgr = ModelManager(db_session)
        model = mgr.create_model(name="fraud-model", task="fraud_detection")
        assert model.id is not None
        assert model.name == "fraud-model"
        assert model.task == "fraud_detection"

        fetched = mgr.get_model(model.id)
        assert fetched.id == model.id

    def test_duplicate_name_raises(self, db_session):
        mgr = ModelManager(db_session)
        mgr.create_model(name="fraud-model")
        with pytest.raises(DuplicateModelNameError):
            mgr.create_model(name="fraud-model")

    def test_get_by_name(self, db_session):
        mgr = ModelManager(db_session)
        mgr.create_model(name="fraud-model")
        assert mgr.get_model_by_name("fraud-model") is not None
        assert mgr.get_model_by_name("missing") is None

    def test_get_not_found(self, db_session):
        mgr = ModelManager(db_session)
        with pytest.raises(ModelNotFoundError):
            mgr.get_model(9999)

    def test_list_models(self, db_session):
        mgr = ModelManager(db_session)
        mgr.create_model(name="a")
        mgr.create_model(name="b")
        assert {m.name for m in mgr.list_models()} == {"a", "b"}


class TestModelVersionCreation:
    def test_create_version_with_lineage(self, db_session):
        _, _, dv = _setup_dataset_version(db_session)
        mgr = ModelManager(db_session)
        model = mgr.create_model(name="fraud-model")
        version = mgr.create_model_version(
            model_id=model.id,
            dataset_version_id=dv.id,
            mlflow_run_id="mlflow-abc",
            artifact_uri="s3://models/fraud-v1.pkl",
            metrics={"f1": 0.9},
        )
        assert version.id is not None
        assert version.version_number == 1
        assert version.state == ModelState.TRAINING
        assert version.dataset_version_id == dv.id
        assert version.mlflow_run_id == "mlflow-abc"
        assert version.metrics_json and "f1" in version.metrics_json

    def test_sequential_version_numbers(self, db_session):
        _, _, dv = _setup_dataset_version(db_session)
        mgr = ModelManager(db_session)
        model = mgr.create_model(name="fraud-model")
        v1 = mgr.create_model_version(model_id=model.id, dataset_version_id=dv.id)
        v2 = mgr.create_model_version(model_id=model.id, dataset_version_id=dv.id)
        assert v1.version_number == 1
        assert v2.version_number == 2

    def test_list_model_versions(self, db_session):
        _, _, dv = _setup_dataset_version(db_session)
        mgr = ModelManager(db_session)
        model = mgr.create_model(name="fraud-model")
        mgr.create_model_version(model_id=model.id, dataset_version_id=dv.id)
        mgr.create_model_version(model_id=model.id, dataset_version_id=dv.id)
        assert len(mgr.list_model_versions(model.id)) == 2

    def test_get_version_not_found(self, db_session):
        mgr = ModelManager(db_session)
        with pytest.raises(ModelVersionNotFoundError):
            mgr.get_model_version(9999)

    def test_update_metrics_merges(self, db_session):
        _, _, dv = _setup_dataset_version(db_session)
        mgr = ModelManager(db_session)
        model = mgr.create_model(name="fraud-model")
        version = mgr.create_model_version(
            model_id=model.id,
            dataset_version_id=dv.id,
            metrics={"f1": 0.9},
        )
        mgr.update_metrics(version.id, {"roc_auc": 0.95})
        metrics = mgr.get_metrics(version.id)
        assert metrics["f1"] == 0.9
        assert metrics["roc_auc"] == 0.95


class TestModelVersionTransitions:
    def _setup(self, db_session):
        _, _, dv = _setup_dataset_version(db_session)
        mgr = ModelManager(db_session)
        model = mgr.create_model(name="fraud-model")
        version = mgr.create_model_version(model_id=model.id, dataset_version_id=dv.id)
        return mgr, model, version

    def test_training_to_candidate(self, db_session):
        mgr, _, v = self._setup(db_session)
        mgr.transition_state(v.id, ModelState.CANDIDATE)
        assert mgr.get_model_version(v.id).state == ModelState.CANDIDATE

    def test_full_promotion_path(self, db_session):
        mgr, _, v = self._setup(db_session)
        for s in [ModelState.CANDIDATE, ModelState.APPROVED, ModelState.PRODUCTION, ModelState.ARCHIVED]:
            mgr.transition_state(v.id, s)
        assert mgr.get_model_version(v.id).state == ModelState.ARCHIVED

    def test_reject_path(self, db_session):
        mgr, _, v = self._setup(db_session)
        mgr.transition_state(v.id, ModelState.CANDIDATE)
        mgr.transition_state(v.id, ModelState.REJECTED)
        assert mgr.get_model_version(v.id).state == ModelState.REJECTED

    @pytest.mark.parametrize(
        "current,target",
        [
            (ModelState.TRAINING, ModelState.PRODUCTION),
            (ModelState.PRODUCTION, ModelState.CANDIDATE),
            (ModelState.ARCHIVED, ModelState.PRODUCTION),
            (ModelState.REJECTED, ModelState.CANDIDATE),
            (ModelState.APPROVED, ModelState.TRAINING),
        ],
    )
    def test_invalid_transitions_rejected(self, db_session, current, target):
        mgr, _, v = self._setup(db_session)
        # Walk to the parametrized ``current`` state through valid steps.
        walk = {
            ModelState.TRAINING: [],
            ModelState.CANDIDATE: [ModelState.CANDIDATE],
            ModelState.APPROVED: [ModelState.CANDIDATE, ModelState.APPROVED],
            ModelState.PRODUCTION: [ModelState.CANDIDATE, ModelState.PRODUCTION],
            ModelState.ARCHIVED: [
                ModelState.CANDIDATE,
                ModelState.APPROVED,
                ModelState.PRODUCTION,
                ModelState.ARCHIVED,
            ],
            ModelState.REJECTED: [ModelState.CANDIDATE, ModelState.REJECTED],
        }
        for step in walk[current]:
            mgr.transition_state(v.id, step)
        with pytest.raises(InvalidModelStateTransitionError):
            mgr.transition_state(v.id, target)

    def test_terminal_states_cannot_transition(self, db_session):
        mgr, _, v = self._setup(db_session)
        mgr.transition_state(v.id, ModelState.REJECTED)
        with pytest.raises(InvalidModelStateTransitionError):
            mgr.transition_state(v.id, ModelState.CANDIDATE)


class TestLineage:
    def test_dataset_version_lineage(self, db_session):
        dm, ds, dv = _setup_dataset_version(db_session)
        mgr = ModelManager(db_session)
        model = mgr.create_model(name="fraud-model")
        version = mgr.create_model_version(model_id=model.id, dataset_version_id=dv.id)
        assert version.dataset_version.id == dv.id
        assert version.dataset_version.dataset_id == ds.id

    def test_training_run_lineage(self, db_session):
        from mlops_framework.training.manager import TrainingManager
        dm, ds, dv = _setup_dataset_version(db_session)
        tm = TrainingManager(db_session, dm)
        run = tm.create_run(dataset_version_id=dv.id, pipeline_id="p")
        tm.start_run(run.id)

        mgr = ModelManager(db_session)
        model = mgr.create_model(name="fraud-model")
        version = mgr.create_model_version(
            model_id=model.id,
            dataset_version_id=dv.id,
            training_run_id=run.id,
        )
        assert version.training_run is not None
        assert version.training_run.id == run.id
        assert version.training_run.dataset_version_id == dv.id
