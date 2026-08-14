"""Alembic environment configuration."""

import os
import sys
from logging.config import fileConfig

from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool
from alembic import context

# Add the project root to the Python path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Load environment variables from the project's .env file so that
# DATABASE_URL (and any other configuration) is available to Alembic
# without hardcoding credentials into alembic.ini.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))

# this is the Alembic Config object
config = context.config

# Override sqlalchemy.url with the value of DATABASE_URL from the environment.
# This is the single source of truth for the database connection string.
database_url = os.getenv("DATABASE_URL")
if not database_url:
    raise RuntimeError(
        "DATABASE_URL is not set. Please ensure it is defined in the "
        "project's .env file or exported in the environment."
    )
config.set_main_option("sqlalchemy.url", database_url)

# Import the Base from the database models
from mlops_framework.database.base import Base
from mlops_framework.database.models.audit_log import AuditLog
from mlops_framework.database.models.dataset import Dataset
from mlops_framework.database.models.dataset_version import DatasetVersion
from mlops_framework.database.models.drift_evaluation import DriftEvaluation
from mlops_framework.database.models.framework_setting import FrameworkSetting
from mlops_framework.database.models.governance_event import GovernanceEvent
from mlops_framework.database.models.model import Model
from mlops_framework.database.models.model_promotion_event import ModelPromotionEvent
from mlops_framework.database.models.model_version import ModelVersion
from mlops_framework.database.models.readiness_evaluation import ReadinessEvaluation
from mlops_framework.database.models.schedule import Schedule
from mlops_framework.database.models.serving_instance import ServingInstance
from mlops_framework.database.models.training_run import TrainingRun

# Interpret the config file for Python logging
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Set the target metadata for autogenerate
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL and not an Engine,
    though an Engine is acceptable here as well. By skipping the Engine
    creation we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the script output.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine and associate a
    connection with the context.
    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
