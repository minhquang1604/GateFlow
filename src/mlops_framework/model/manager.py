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
    rollback_to(version_id) -> RollbackResult
    update_metrics(version_id, metrics) -> ModelVersion
"""

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from mlops_framework.database.models.model import Model
from mlops_framework.database.models.model_version import (
    ModelState,
    ModelVersion,
)
from mlops_framework.exceptions import (
    ConcurrentPromotionError,
    DuplicateModelNameError,
    ModelNotFoundError,
    ModelVersionNotFoundError,
    RollbackError,
)
from mlops_framework.model.lifecycle import validate_transition


@dataclass
class RollbackResult:
    """What a rollback changed.

    ``previous_production_id`` is ``None`` when the model had no
    PRODUCTION version at the time — rolling *into* an empty slot is
    allowed (the incumbent may already have been archived by hand), and
    the caller usually wants to know that is what happened.
    """

    model_id: int
    model_name: str
    restored_version_id: int
    restored_version_number: int
    previous_production_id: int | None
    previous_production_number: int | None


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
        try:
            self._session.flush()
        except IntegrityError as exc:
            # validate_transition only checks this row's own state machine —
            # it cannot see a concurrent writer promoting a different
            # version of the same model in another transaction. The
            # database's partial unique index
            # (uq_model_versions_one_production_per_model) is the actual
            # guarantee; surface it as a clear framework error instead of a
            # raw IntegrityError. Session cleanup (rollback) is the caller's
            # responsibility — see database/session.py's get_session() and
            # api/deps.py — managers only ever flush().
            if target == ModelState.PRODUCTION:
                raise ConcurrentPromotionError(
                    f"model_version {version_id} could not be promoted to "
                    "PRODUCTION: another version of this model was promoted "
                    "concurrently"
                ) from exc
            raise
        return version

    def rollback_to(self, version_id: int) -> RollbackResult:
        """Put a previously-retired version back into production.

        The recovery path the registry did not have: a model that
        reached PRODUCTION and turned out to be bad could only be
        replaced by training a new one, because ARCHIVED was terminal.

        Deliberately *not* routed through
        :class:`~mlops_framework.governance.promotion.ModelPromotionPolicy`.
        That policy answers "is this candidate good enough to replace
        production", judged on metrics. A rollback answers a different
        question — "production is broken, put back the version that
        worked" — and the version being restored has already been
        through the policy once. Gating it on metrics would block the
        rollback in exactly the case it exists for: an incumbent whose
        offline metrics look better than the version you need back.
        The decision is an operator's; this method records it loudly
        rather than second-guessing it.

        Ordering matches promotion's: the incumbent is archived *before*
        the restored version is promoted, because
        ``uq_model_versions_one_production_per_model`` permits no window
        with two.

        Args:
            version_id: the ModelVersion to restore. Must belong to a
                model, be ARCHIVED or APPROVED, and not already be the
                production version.

        Returns:
            :class:`RollbackResult` naming both sides of the swap.

        Raises:
            ModelVersionNotFoundError: no such version.
            RollbackError: the version is not in a state a rollback can
                restore from, or is already in production.
        """
        target = self.get_model_version(version_id)
        model = self.get_model(target.model_id)

        if target.state == ModelState.PRODUCTION:
            raise RollbackError(
                f"model_version {version_id} is already the PRODUCTION "
                f"version of {model.name!r}"
            )
        if target.state not in {ModelState.ARCHIVED, ModelState.APPROVED}:
            raise RollbackError(
                f"model_version {version_id} is {target.state.value}; only an "
                "ARCHIVED or APPROVED version can be rolled back to. A "
                "REJECTED version failed the promotion policy and a "
                "CANDIDATE has never been in production."
            )

        current = self._session.execute(
            select(ModelVersion).where(
                ModelVersion.model_id == model.id,
                ModelVersion.state == ModelState.PRODUCTION,
            )
        ).scalars().first()

        previous_id = current.id if current is not None else None
        previous_number = current.version_number if current is not None else None

        if current is not None:
            self.transition_state(current.id, ModelState.ARCHIVED)
        if target.state == ModelState.ARCHIVED:
            self.transition_state(target.id, ModelState.APPROVED)
        self.transition_state(target.id, ModelState.PRODUCTION)

        return RollbackResult(
            model_id=model.id,
            model_name=model.name,
            restored_version_id=target.id,
            restored_version_number=target.version_number,
            previous_production_id=previous_id,
            previous_production_number=previous_number,
        )

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
