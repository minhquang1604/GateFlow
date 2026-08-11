"""Model manager: register models and manage ModelVersion lifecycle.

Public API:
    create_model(name, description=None, task=None) -> Model
    get_model(model_id) -> Model
    get_model_by_name(name) -> Optional[Model]
    list_models() -> list[Model]
    create_model_version(model_id, dataset_version_id, training_run_id=None,
                        mlflow_run_id=None, artifact_uri=None, metrics=None)
    get_model_version(version_id) -> ModelVersion
    list_model_versions(model_id) -> list[ModelVersion]
    transition_state(version_id, new_state) -> ModelVersion
    update_metrics(version_id, metrics) -> ModelVersion
"""

import json
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mlops_framework.database.models.model import Model
from mlops_framework.database.models.model_version import (
    ModelState,
    ModelVersion,
)
from mlops_framework.exceptions import (
    DuplicateModelNameError,
    ModelNotFoundError,
    ModelVersionNotFoundError,
)
from mlops_framework.model.lifecycle import validate_transition


class ModelManager:
    """Manages Model and ModelVersion entities."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # ------------------------------------------------------------------ #
    # Model
    # ------------------------------------------------------------------ #

    def create_model(
        self,
        name: str,
        description: str | None = None,
        task: str | None = None,
    ) -> Model:
        existing = self._session.execute(
            select(Model).where(Model.name == name)
        ).scalar_one_or_none()
        if existing is not None:
            raise DuplicateModelNameError(f"Model with name '{name}' already exists")
        model = Model(name=name, description=description, task=task)
        self._session.add(model)
        self._session.flush()
        return model

    def get_model(self, model_id: int) -> Model:
        model = self._session.get(Model, model_id)
        if model is None:
            raise ModelNotFoundError(f"Model with id {model_id} not found")
        return model

    def get_model_by_name(self, name: str) -> Model | None:
        return self._session.execute(
            select(Model).where(Model.name == name)
        ).scalar_one_or_none()

    def list_models(self) -> list[Model]:
        return list(self._session.execute(select(Model)).scalars().all())

    # ------------------------------------------------------------------ #
    # ModelVersion
    # ------------------------------------------------------------------ #

    def _next_version_number(self, model_id: int) -> int:
        result = self._session.execute(
            select(func.max(ModelVersion.version_number))
            .where(ModelVersion.model_id == model_id)
        ).scalar()
        return (result or 0) + 1

    def create_model_version(
        self,
        model_id: int,
        dataset_version_id: int,
        training_run_id: int | None = None,
        mlflow_run_id: str | None = None,
        artifact_uri: str | None = None,
        metrics: dict[str, Any] | None = None,
        state: ModelState = ModelState.TRAINING,
        notes: str | None = None,
    ) -> ModelVersion:
        # Verify model exists.
        self.get_model(model_id)
        version_number = self._next_version_number(model_id)
        version = ModelVersion(
            model_id=model_id,
            dataset_version_id=dataset_version_id,
            training_run_id=training_run_id,
            version_number=version_number,
            state=state,
            mlflow_run_id=mlflow_run_id,
            artifact_uri=artifact_uri,
            metrics_json=json.dumps(metrics) if metrics else None,
            notes=notes,
        )
        self._session.add(version)
        self._session.flush()
        return version

    def get_model_version(self, version_id: int) -> ModelVersion:
        version = self._session.get(ModelVersion, version_id)
        if version is None:
            raise ModelVersionNotFoundError(
                f"ModelVersion with id {version_id} not found"
            )
        return version

    def list_model_versions(self, model_id: int) -> list[ModelVersion]:
        return list(
            self._session.execute(
                select(ModelVersion)
                .where(ModelVersion.model_id == model_id)
                .order_by(ModelVersion.version_number)
            ).scalars().all()
        )

    def transition_state(self, version_id: int, new_state: "str | ModelState") -> ModelVersion:
        version = self.get_model_version(version_id)
        if isinstance(new_state, str):
            try:
                target = ModelState(new_state.upper())
            except ValueError as exc:
                raise ValueError(f"Invalid state: {new_state}") from exc
        else:
            target = new_state
        validate_transition(version.state, target)
        version.state = target
        self._session.flush()
        return version

    def update_metrics(self, version_id: int, metrics: dict[str, Any]) -> ModelVersion:
        version = self.get_model_version(version_id)
        existing: dict[str, Any] = {}
        if version.metrics_json:
            existing = json.loads(version.metrics_json)
        existing.update(metrics)
        version.metrics_json = json.dumps(existing)
        self._session.flush()
        return version

    def get_metrics(self, version_id: int) -> dict[str, Any]:
        version = self.get_model_version(version_id)
        if not version.metrics_json:
            return {}
        return json.loads(version.metrics_json)
