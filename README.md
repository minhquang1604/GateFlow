# MLOps Framework

A reusable MLOps Framework for managing datasets, training runs, and model lifecycle.

## Overview

This framework provides a clean, modular architecture for:

- **Dataset Management**: Create and manage logical datasets with immutable versioning
- **Dataset Versioning**: Track dataset snapshots with checksums and schema hashes
- **Training Run Tracking**: Manage training executions linked to specific dataset versions
- **Metadata Persistence**: Store and query metadata for reproducibility

## Architecture

The framework follows a clean layered architecture:

```
src/mlops_framework/
├── config/          # Configuration management
├── database/        # Database models and session management
├── dataset/         # Dataset management logic
├── training/        # Training run management logic
├── model/          # Model management (placeholder)
├── pipeline/       # Pipeline management (placeholder)
├── governance/     # Governance policies (placeholder)
└── lineage/        # Lineage tracking (placeholder)
```

### Layer Separation

- **Domain Layer**: Core business entities (Dataset, DatasetVersion, TrainingRun)
- **Application Layer**: Managers containing business logic
- **Infrastructure Layer**: Database models and session management
- **Persistence Layer**: SQLAlchemy ORM models

## Installation

```bash
# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install in development mode
pip install -e ".[dev]"
```

## Database Setup

### Using Docker Compose (PostgreSQL)

```bash
# Copy environment file
cp .env.example .env

# Start PostgreSQL
docker compose up -d

# Run migrations
alembic upgrade head
```

### Using SQLite (Testing)

The framework automatically uses SQLite when `DATABASE_URL=sqlite:///./test.db`.

## Database Migrations

```bash
# Create a new migration
alembic revision --autogenerate -m "description"

# Apply migrations
alembic upgrade head

# Check current version
alembic current

# Rollback one version
alembic downgrade -1
```

## Usage

### Creating a Dataset

```python
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from mlops_framework.dataset import DatasetManager
from mlops_framework.training import TrainingManager

# Create database session
engine = create_engine("sqlite:///./mlops.db")
Session = sessionmaker(bind=engine)
session = Session()

# Create dataset
dataset_manager = DatasetManager(session)
dataset = dataset_manager.create_dataset(
    name="fraud-detection",
    description="Credit card fraud detection dataset"
)
session.commit()
```

### Creating a Dataset Version

```python
# Create version
version = dataset_manager.create_version(
    dataset_id=dataset.id,
    storage_uri="s3://bucket/data/v1.csv",
    row_count=100000,
    metadata={
        "columns": [
            {"name": "transaction_id", "dtype": "int64"},
            {"name": "amount", "dtype": "float64"},
            {"name": "is_fraud", "dtype": "int64"},
        ]
    }
)
session.commit()

print(f"Created version {version.version_number}")
print(f"Checksum: {version.checksum}")
print(f"Schema hash: {version.schema_hash}")
```

### Creating a Training Run

```python
# Create training run
training_manager = TrainingManager(session, dataset_manager)
run = training_manager.create_run(
    dataset_version_id=version.id,
    trigger_type="MANUAL",
    metadata={"model_type": "xgboost", "n_estimators": 100}
)
session.commit()

# Update run status
run = training_manager.update_status(run.id, "RUNNING")
session.commit()

# Complete the run
run = training_manager.update_status(run.id, "SUCCESS")
session.commit()
```

### Querying the Complete Flow

```python
# Query Dataset -> DatasetVersion -> TrainingRun
dataset = dataset_manager.get_dataset(dataset_id)
versions = dataset_manager.list_versions(dataset_id)

for version in versions:
    print(f"Version {version.version_number}")
    runs = training_manager.list_runs(dataset_version_id=version.id)
    for run in runs:
        print(f"  Run {run.id}: {run.status.value}")
```

## Configuration

Environment variables (see `.env.example`):

| Variable | Description | Default |
|----------|-------------|---------|
| `DATABASE_URL` | Database connection URL | `postgresql+psycopg://postgres@localhost:5432/mlops_framework` |
| `DATABASE_POOL_SIZE` | Connection pool size | 5 |
| `DATABASE_ECHO` | Echo SQL queries | false |
| `DEBUG` | Enable debug mode | false |

## Running Tests

```bash
# Run all tests
pytest

# Run specific test file
pytest tests/integration/test_complete_flow.py

# Run with coverage
pytest --cov=mlops_framework
```

## Project Structure

```
mlops-framework/
├── src/mlops_framework/
│   ├── __init__.py
│   ├── config/
│   │   ├── __init__.py
│   │   └── settings.py
│   ├── database/
│   │   ├── __init__.py
│   │   ├── base.py
│   │   ├── session.py
│   │   └── models/
│   │       ├── __init__.py
│   │       ├── dataset.py
│   │       ├── dataset_version.py
│   │       └── training_run.py
│   ├── dataset/
│   │   ├── __init__.py
│   │   ├── manager.py
│   │   ├── versioning.py
│   │   ├── checksum.py
│   │   └── schemas.py
│   ├── training/
│   │   ├── __init__.py
│   │   ├── manager.py
│   │   └── schemas.py
│   ├── model/         # Placeholder
│   ├── pipeline/      # Placeholder
│   ├── governance/    # Placeholder
│   ├── lineage/      # Placeholder
│   └── exceptions.py
├── tests/
│   ├── unit/
│   └── integration/
├── alembic/
│   ├── versions/
│   ├── env.py
│   └── alembic.ini
├── docker-compose.yml
├── pyproject.toml
├── .env.example
└── README.md
```

## Database ERD

```
┌─────────────┐       ┌───────────────────┐       ┌───────────────┐
│  datasets   │       │ dataset_versions  │       │ training_runs │
├─────────────┤       ├───────────────────┤       ├───────────────┤
│ id (PK)     │──1:N──│ id (PK)           │──1:N──│ id (PK)       │
│ name (U)    │       │ dataset_id (FK)   │       │ dataset_ver   │
│ description │       │ version_number    │       │   _id (FK)    │
│ created_at  │       │ storage_uri       │       │ status        │
│ updated_at  │       │ checksum          │       │ trigger_type  │
└─────────────┘       │ schema_hash       │       │ started_at    │
                      │ row_count         │       │ completed_at  │
                      │ metadata_json     │       │ metadata_json │
                      │ is_immutable      │       │ created_at    │
                      │ created_at        │       │ updated_at    │
                      │ updated_at        │       └───────────────┘
                      └───────────────────┘
```

## Week 1 Features

### Implemented

- [x] SQLAlchemy 2.x ORM models
- [x] PostgreSQL/SQLite database support
- [x] Alembic migrations
- [x] Dataset CRUD operations
- [x] Immutable dataset versioning
- [x] Deterministic SHA-256 checksums
- [x] Deterministic schema hashing
- [x] Training run management
- [x] Status transition validation
- [x] Complete Dataset -> DatasetVersion -> TrainingRun flow
- [x] Integration tests

### Planned for Week 2+

- [ ] LocalDockerOrchestrator for running training jobs
- [ ] AirflowOrchestrator for workflow orchestration
- [ ] MLflowTracker for experiment tracking
- [ ] Model registry and versioning
- [ ] Readiness engine
- [ ] Policy engine
- [ ] Lineage tracking

## License

MIT
