"""Seed the demo SQLite database using the Fraud Detection case study app.

The case study's own ``main()`` is the cleanest possible demo: it uses
only the public SDK, registers both pipelines, creates a dataset + a
version, runs a training pass, and prints the resulting lineage. We run
it twice (baseline + advanced) so the UI has data to show.

Run with the same DATABASE_URL the uvicorn process is using::

    DATABASE_URL="sqlite:///./mlops_demo.db" .venv/bin/python seed_demo.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Make src/ and case_studies/ importable
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "src"))
sys.path.insert(0, str(HERE))

# Force the global settings cache to load with our env var
os.environ.setdefault("DATABASE_URL", "sqlite:///./mlops_demo.db")
print(f"Using DATABASE_URL={os.environ['DATABASE_URL']}")


from mlops_framework.config.settings import get_settings
from mlops_framework.database.base import Base
from mlops_framework.database.session import DatabaseManager, set_db_manager

get_settings.cache_clear()

# Step 1 — create schema with a one-off engine we dispose immediately
db_path = str(HERE / "mlops_demo.db")
from sqlalchemy import create_engine

schema_eng = create_engine(f"sqlite:///{db_path}")
Base.metadata.create_all(schema_eng)
schema_eng.dispose()
del schema_eng
print("✓ Schema created")

# Step 2 — install a DatabaseManager bound to the demo DB and run the
# case study app's main() once for each pipeline.
mgr = DatabaseManager(database_url=os.environ["DATABASE_URL"])
set_db_manager(mgr)

from case_studies.fraud_detection.app import main as fraud_main

print("\n[1/2] Running fraud-baseline end-to-end via the SDK ...")
try:
    fraud_main()
    print("✓ baseline lifecycle complete")
except Exception as e:
    print(f"✗ baseline failed: {e!r}")
    raise

print("\n[2/2] Running fraud-advanced end-to-end via the SDK ...")
# The case study's main() always uses the baseline pipeline; we patch
# the constant in-place to drive the advanced pipeline on the second
# pass. The dataset & model are reused.
import case_studies.fraud_detection.app as fraud_app

fraud_app.PIPELINE = "fraud-advanced"  # type: ignore[attr-defined]

# Wrap main() to override the default pipeline
def main_advanced():
    """Same as fraud_main, but uses fraud-advanced pipeline."""
    here = Path(fraud_app.__file__).parent
    data_path = here / "data" / "transactions.csv"
    if not data_path.exists():
        from case_studies.fraud_detection import data as _data
        _data.write_csv(data_path, n_rows=5000)

    project = fraud_app.build_project()
    try:
        fraud_app.ensure_dataset(project, data_path, n_rows=5000)
        fraud_app.ensure_model(project)
        ds = project.get_dataset(fraud_app.DATASET_NAME)
        v = ds.latest_version
        assert v is not None
        run = project.train(
            dataset_version=v,
            pipeline="fraud-advanced",
            parameters={"seed": 7, "n_estimators": 100},
            wait=True,
            timeout=60,
        )
        print(f"  Advanced run: id={run.id} status={run.status} metrics={run.metrics}")
    finally:
        project.orchestrator.shutdown()

main_advanced()
print("✓ advanced lifecycle complete")

# Step 3 — promote the latest model version to PRODUCTION so the UI's
# Models page has a starred row.
print("\nPromoting latest model version to PRODUCTION ...")
from mlops_framework.database.models.model_version import ModelState
from mlops_framework.lineage.manager import LineageManager
from mlops_framework.model.manager import ModelManager

with mgr.get_session() as s:
    mm = ModelManager(s)
    m = mm.get_model_by_name("fraud-xgboost")
    if m is not None:
        versions = mm.list_model_versions(m.id)
        latest = max(versions, key=lambda x: x.version_number)
        mm.update_model_version_state(latest.id, ModelState.PRODUCTION)
        s.commit()
        # Add a dataset→model edge for the lineage graph
        lm = LineageManager(s)
        dv = s.execute(
            __import__("sqlalchemy").text("SELECT dataset_version_id FROM model_versions WHERE id = :id"),
            {"id": latest.id},
        ).first()
        if dv and dv[0]:
            lm.record_dataset_to_model(dv[0], latest.id)
            s.commit()
        print(f"✓ Model version {latest.version_number} (id={latest.id}) → PRODUCTION")
    else:
        print("  (model 'fraud-xgboost' not found — skipping promotion)")

# Final summary
print("\n" + "=" * 60)
print("Demo data ready! Open http://127.0.0.1:8000/ in your browser.")
print("=" * 60)
