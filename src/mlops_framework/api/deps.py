"""FastAPI dependency providers.

Centralizes the creation of database sessions and manager instances so every
router uses the same configuration.
"""

from __future__ import annotations

from typing import Iterator

from fastapi import Depends
from sqlalchemy.orm import Session

from mlops_framework.database.session import (
    DatabaseManager,
    get_db_manager,
)
from mlops_framework.dataset.manager import DatasetManager
from mlops_framework.model.manager import ModelManager
from mlops_framework.training.manager import TrainingManager


def get_db_manager_dep() -> DatabaseManager:
    """FastAPI dependency for the global :class:`DatabaseManager`."""
    return get_db_manager()


def get_db(
    manager: DatabaseManager = Depends(get_db_manager_dep),
) -> Iterator[Session]:
    """Yield a database session, closing it after the request.

    Mirrors the behaviour of :func:`mlops_framework.database.session.get_session`
    but is wired as a FastAPI dependency so request handlers can declare
    ``db: Session = Depends(get_db)`` and stay free of context-manager noise.
    """
    session = manager.session_factory()
    try:
        yield session
    finally:
        session.close()


def get_dataset_manager(db: Session = Depends(get_db)) -> DatasetManager:
    """FastAPI dependency for :class:`DatasetManager`."""
    return DatasetManager(db)


def get_training_manager(
    db: Session = Depends(get_db),
    dm: DatasetManager = Depends(get_dataset_manager),
) -> TrainingManager:
    """FastAPI dependency for :class:`TrainingManager`."""
    return TrainingManager(db, dm)


def get_model_manager(db: Session = Depends(get_db)) -> ModelManager:
    """FastAPI dependency for :class:`ModelManager`."""
    return ModelManager(db)
