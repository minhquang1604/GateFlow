"""Airflow DAG: read two dataset versions and ask the framework whether
they have drifted.

Why this DAG exists at all
--------------------------
The framework deliberately does not read dataset files. ``DriftService``
takes feature values from its caller, and nothing under ``src/`` opens
an S3 object or a CSV. That boundary is why drift could only ever be
evaluated from a Python script that had the data in hand — a console
button had nowhere to get it from, and giving the app container S3
credentials plus a 144 MB CSV inside a 256 MiB reservation is the exact
shape of failure that already killed Airflow's own gunicorn worker once
(see ``mlops_framework.orchestration.airflow``'s module docstring).

So the work happens where the data already is. Airflow reads datasets
for training today; this DAG reads them for drift.

What it does *not* do
---------------------
It does not decide anything. It reads, samples, and posts values to
``POST /api/internal/drift`` — the framework picks the detector, applies
the configured thresholds, reaches the verdict and persists the
DriftEvaluation row. Same split as ``resolve_context``/``readiness`` in
``mlops_training_pipeline.py``: a DAG that computed its own verdict
could assert anything, and the row would be a client's claim rather
than the framework's conclusion.

Sampling is the one judgement call left here, and it is about transport,
not statistics: a KS test settles on a few thousand points, so shipping
284,807 values per feature over HTTP would reach the same answer more
slowly. ``sample_size`` arrives in ``dag_run.conf`` (the API's default
is 5000) and the sample is deterministic per run, so a re-run of the
same dag_run compares the same rows.

Triggered by ``POST /api/drift/{version_id}/check``; never scheduled.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any

from airflow import DAG
from airflow.operators.python import PythonOperator

DEFAULT_ARGS = {
    "owner": "mlops-framework",
    "depends_on_past": False,
    "retries": 0,
    "execution_timeout": timedelta(minutes=30),
}

APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://app:8000")

# Columns that are never features: the label, and bookkeeping columns a
# drift test on would be meaningless or actively misleading (an
# ever-increasing "time" column drifts by construction on every new
# batch, which would make every check positive).
_NON_FEATURE_COLUMNS = {"class", "label", "target", "y", "time", "timestamp", "id"}


def _internal_headers() -> dict[str, str]:
    """Auth header for ``/api/internal/*`` — see
    ``mlops_training_pipeline.py``'s identical helper and
    ``mlops_framework.api.security``."""
    token = os.environ.get("CONSOLE_WRITE_TOKEN", "")
    headers = {"X-Actor": "airflow:mlops_drift_check"}
    if token:
        headers["X-Console-Token"] = token
    return headers


def _read_context(run_id: int) -> dict[str, Any]:
    import httpx

    response = httpx.get(
        f"{APP_BASE_URL}/api/internal/dataset-versions/{run_id}",
        headers=_internal_headers(),
        timeout=30.0,
    )
    response.raise_for_status()
    return response.json()


def _sample_features(storage_uri: str, sample_size: int, seed: int) -> dict[str, list[float]]:
    """Read a dataset and return ``{feature: [values]}``, sampled.

    Only numeric columns are returned: the framework's reference
    detector runs a KS test on numeric features and a chi-square on
    categorical ones, and mixing the two into one mapping would leave
    the detector guessing which is which. Categorical drift is a
    follow-up, not something to fake here.
    """
    import pandas as pd

    df = pd.read_csv(storage_uri)
    numeric = df.select_dtypes(include="number")
    keep = [c for c in numeric.columns if str(c).lower() not in _NON_FEATURE_COLUMNS]
    numeric = numeric[keep]

    if len(numeric) > sample_size:
        # random_state from the dag_run, so re-running the same run
        # compares the same rows rather than quietly moving the goalposts.
        numeric = numeric.sample(n=sample_size, random_state=seed)

    return {
        str(column): [float(v) for v in numeric[column].dropna().tolist()]
        for column in numeric.columns
    }


def check_drift(**context: Any) -> dict[str, Any]:
    """Read both versions, sample them, and let the framework decide."""
    import httpx

    conf = (context.get("dag_run") or {}).conf or {}
    reference_id = int(conf["reference_version_id"])
    current_id = int(conf["current_version_id"])
    sample_size = int(conf.get("sample_size", 5000))
    # Stable per dag_run, so a retry of the same run resamples identically.
    seed = abs(hash(str(context["dag_run"].run_id))) % (2**31)

    reference = _read_context(reference_id)
    current = _read_context(current_id)
    print(
        f"[airflow] drift: reference v{reference['version_number']} "
        f"({reference['storage_uri']}) vs current v{current['version_number']} "
        f"({current['storage_uri']}), sample_size={sample_size}"
    )

    reference_data = _sample_features(reference["storage_uri"], sample_size, seed)
    current_data = _sample_features(current["storage_uri"], sample_size, seed)

    shared = sorted(set(reference_data) & set(current_data))
    if not shared:
        raise RuntimeError(
            "the two versions share no numeric feature columns — nothing to "
            f"compare (reference: {sorted(reference_data)[:5]}…, "
            f"current: {sorted(current_data)[:5]}…)"
        )
    # Only the intersection: a feature present on one side alone has no
    # reference distribution, and passing it would have the detector
    # compare a column against nothing.
    reference_data = {k: reference_data[k] for k in shared}
    current_data = {k: current_data[k] for k in shared}
    print(f"[airflow] comparing {len(shared)} shared numeric features")

    response = httpx.post(
        f"{APP_BASE_URL}/api/internal/drift",
        json={
            "reference_dataset_version_id": reference_id,
            "current_dataset_version_id": current_id,
            "reference_data": reference_data,
            "current_data": current_data,
            "notes": (
                f"triggered via Airflow ({context['dag_run'].run_id}), "
                f"{len(shared)} features, sample_size={sample_size}"
            ),
        },
        headers=_internal_headers(),
        timeout=120.0,
    )
    response.raise_for_status()
    result = response.json()
    print(
        f"[airflow] verdict: drift_detected={result['drift_detected']} "
        f"score={result['score']:.4f} threshold={result['threshold']:.4f}"
    )
    return result


with DAG(
    dag_id="mlops_drift_check",
    description="Read two dataset versions and ask the framework whether they drifted",
    default_args=DEFAULT_ARGS,
    start_date=datetime(2024, 1, 1),
    schedule=None,  # triggered by POST /api/drift/{id}/check, never scheduled
    catchup=False,
    max_active_runs=4,
    tags=["mlops-framework", "drift"],
) as dag:
    PythonOperator(task_id="check_drift", python_callable=check_drift)
