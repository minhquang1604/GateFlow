"""Fixtures for the closed-loop demo tests.

The demo's happy path runs against real Airflow, MLflow and (optionally)
Telegram, so CI cannot execute it end to end. What CI *can* verify is
everything the demo decides — which is the part that matters. These
fixtures give each test a real SQLite database, real generated CSVs, and
a context seeded to whatever point in the lifecycle the test is about,
so the assertions are about persisted state transitions rather than
about return values or printed text.

Scale is reduced (400 training rows, 200-row windows) purely for speed.
The seeds, the shift parameters, and every threshold are the demo's own,
because a test that passes only at a scale the demo never runs at is not
testing the demo.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
for path in (str(ROOT), str(ROOT / "src")):
    if path not in sys.path:
        sys.path.insert(0, path)

from case_studies.fraud_detection import data as fraud_data  # noqa: E402
from demo.config import DemoConfig  # noqa: E402
from demo.context import DemoContext  # noqa: E402
from mlops_framework.database.base import Base  # noqa: E402
from mlops_framework.database.models.model_version import ModelState  # noqa: E402
from mlops_framework.database.session import DatabaseManager  # noqa: E402
from mlops_framework.dataset.checksum import calculate_file_checksum  # noqa: E402
from mlops_framework.dataset.manager import DatasetManager  # noqa: E402
from mlops_framework.model.manager import ModelManager  # noqa: E402
from mlops_framework.training.manager import TrainingManager  # noqa: E402

#: A real, importable pipeline that trains deterministically without
#: touching MLflow, Airflow or the network.
TEST_PIPELINE = "tests._pipelines.e2e_training:main"


@pytest.fixture
def demo_config(tmp_path: Path) -> DemoConfig:
    """The demo's own parameters, at test scale and in a temp directory."""
    return DemoConfig(
        n_rows=400,
        window_rows=200,
        data_dir=tmp_path / "data",
        # Identical to the runtime path: with the bind mount, the demo
        # container writes and Airflow reads the same location.
        airflow_data_dir=str(tmp_path / "data"),
        dag_timeout=60.0,
        pipeline_id=TEST_PIPELINE,
        dag_id=TEST_PIPELINE,
        # The demo's own 1000-row bar would block every version at this
        # scale. Stating a test-scale bar explicitly is the point of
        # having it in the config at all.
        required_rows=300,
    )


@pytest.fixture
def ctx(tmp_path: Path, demo_config: DemoConfig) -> DemoContext:
    """A context with an empty database and no lifecycle state."""
    demo_config.data_dir.mkdir(parents=True, exist_ok=True)
    db = DatabaseManager(f"sqlite:///{tmp_path / 'demo.db'}")
    Base.metadata.create_all(db.engine)
    return DemoContext(
        db=db,
        config=demo_config,
        settings=_FakeSettings(),
        endpoints={
            "mlflow_uri": "http://mlflow.invalid",
            "mlflow_uri_for_airflow": "http://mlflow.invalid",
            "airflow_url": "http://airflow.invalid",
            "serving_url": "http://serving.invalid",
        },
    )


@pytest.fixture
def ctx_with_v1(ctx: DemoContext) -> DemoContext:
    """A context at the point Phase 1 leaves it.

    Dataset V1 registered and written to disk, Model V1 in PRODUCTION.
    Seeded directly rather than by running the training phase, which
    needs a real Airflow DAG — the phase itself is covered by
    ``tests/integration/test_end_to_end_demo_logic.py``.
    """
    cfg = ctx.config
    v1_path = cfg.local_path(cfg.v1_filename)
    fraud_data.write_csv(
        v1_path,
        n_rows=cfg.n_rows,
        fraud_ratio=cfg.fraud_ratio,
        seed=cfg.seed,
        drift_shift=0.0,
    )
    profile = fraud_data.describe_csv(v1_path)
    metadata = dict(profile["metadata"])
    metadata["content_sha256"] = calculate_file_checksum(v1_path)

    with ctx.db.get_session() as session:
        dm = DatasetManager(session)
        dataset = dm.create_dataset(cfg.dataset_name, description="test")
        version = dm.create_version(
            dataset_id=dataset.id,
            storage_uri=cfg.airflow_path(cfg.v1_filename),
            row_count=profile["row_count"],
            metadata=metadata,
        )
        tm = TrainingManager(session, dm)
        run = tm.create_run(
            dataset_version_id=version.id, pipeline_id=cfg.pipeline_id
        )
        tm.start_run(run.id)
        tm.complete_run(run.id)

        mm = ModelManager(session)
        model = mm.create_model(name=cfg.model_name, task="fraud_detection")
        mv = mm.create_model_version(
            model_id=model.id,
            dataset_version_id=version.id,
            training_run_id=run.id,
            metrics={"f1": 0.92, "precision": 0.90, "recall": 0.94},
            state=ModelState.CANDIDATE,
        )
        mm.transition_state(mv.id, ModelState.APPROVED)
        mm.transition_state(mv.id, ModelState.PRODUCTION)
        session.commit()

        ctx.dataset_id = dataset.id
        ctx.v1_version_id = version.id
        ctx.model_id = model.id
        ctx.v1_model_version_id = mv.id

    ctx.state.dataset_version = "dataset_v1"
    ctx.state.dataset_version_id = ctx.v1_version_id
    ctx.state.model_version = "model_v1"
    ctx.state.model_version_id = ctx.v1_model_version_id
    ctx.state.model_state = ModelState.PRODUCTION.value
    return ctx


@pytest.fixture
def ctx_drifted(ctx_with_v1: DemoContext) -> DemoContext:
    """A context at the point drift has been detected and persisted."""
    from demo.steps import detect_drift, inject_drift

    inject_drift.run(ctx_with_v1)
    result = detect_drift.run(ctx_with_v1)
    ctx_with_v1.drift_result = result  # type: ignore[attr-defined]
    return ctx_with_v1


class _FakeSettings:
    """Just the attributes the demo actually reads off Settings."""

    database_url = "sqlite://"
    telegram_bot_token = ""
    telegram_admin_chat_id = ""
    telegram_approval_timeout_seconds = 5.0
    airflow_username = "airflow"
    airflow_password = "airflow"
