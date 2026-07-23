"""Sample Airflow DAG that runs a training pipeline.

This DAG is the production-side counterpart of the framework's
``LocalDockerOrchestrator`` test pipelines. The framework does not
import this file; Airflow does. The DAG receives a config blob through
``dag_run.conf`` and forwards it to a training entry point.

The DAG is intentionally minimal so it can be triggered, observed and
cancelled via the framework's ``AirflowOrchestrator`` adapter.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from airflow import DAG
from airflow.operators.python import PythonOperator


DEFAULT_ARGS = {
    "owner": "mlops-framework",
    "depends_on_past": False,
    "retries": 0,
    "retry_delay": timedelta(minutes=1),
}


def _run_training(config: dict[str, Any]) -> dict[str, Any]:
    """Single task that pretends to train a model.

    In a real deployment this would shell out to a training script or
    invoke a remote runner. For demonstration we just log the config
    and return a small payload.
    """
    return {
        "status": "SUCCESS",
        "training_run_id": config.get("training_run_id"),
        "dataset_version_id": config.get("dataset_version_id"),
        "completed_at": datetime.utcnow().isoformat(),
    }


with DAG(
    dag_id="mlops_training_pipeline",
    default_args=DEFAULT_ARGS,
    description="Sample training pipeline triggered by mlops_framework",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
    tags=["mlops", "framework"],
) as dag:
    train = PythonOperator(
        task_id="train",
        python_callable=_run_training,
        op_kwargs={"config": "{{ dag_run.conf or {} }}"},
    )
