"""Pytest configuration and fixtures for integration tests."""


import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# Registers every table on Base.metadata for create_all() below —
# see the models package docstring.
from mlops_framework.database import models  # noqa: F401
from mlops_framework.database.base import Base


@pytest.fixture(scope="function")
def db_engine():
    """Create a test database engine using SQLite in-memory."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture(scope="function")
def db_session(db_engine):
    """Create a test database session."""
    session_factory = sessionmaker(bind=db_engine)
    session = session_factory()
    yield session
    session.rollback()
    session.close()


@pytest.fixture
def sample_dataset_data():
    """Sample dataset data for testing."""
    return {
        "name": "test-dataset",
        "description": "A test dataset",
    }


@pytest.fixture
def sample_dataset_version_data():
    """Sample dataset version data for testing."""
    return {
        "storage_uri": "s3://bucket/data.csv",
        "row_count": 1000,
        "metadata": {
            "columns": [
                {"name": "id", "dtype": "int64"},
                {"name": "name", "dtype": "object"},
                {"name": "value", "dtype": "float64"},
            ],
            "source": "test",
        },
    }
