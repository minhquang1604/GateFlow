"""Pipelines used by the Customer Churn case study.

Called by the orchestrator. No framework imports — only pure work and
a return dict captured on stdout.

Three pipelines:

* :func:`train_baseline` — a trivial model.
* :func:`train_balanced` — adds class-weight adjustment.
* :func:`fail` — used in tests to verify the SDK surfaces errors.
"""

from __future__ import annotations

import os
import tempfile


def train_baseline(config: dict) -> dict:
    run_id = config.get("training_run_id") or 0
    accuracy = round(0.78 + 0.001 * ((run_id + 1) % 30), 4)
    f1 = round(0.55 + 0.001 * ((run_id + 1) % 40), 4)

    tmpdir = tempfile.mkdtemp(prefix="churn-artifact-")
    artifact_path = os.path.join(tmpdir, "model.txt")
    with open(artifact_path, "w") as f:
        f.write(f"churn-baseline v1\naccuracy={accuracy}\nf1={f1}\n")

    return {
        "status": "SUCCESS",
        "metrics": {"accuracy": accuracy, "f1": f1},
        "artifact_path": artifact_path,
        "pipeline": "churn-baseline",
    }


def train_balanced(config: dict) -> dict:
    """Same skeleton, class-balanced variant. Demonstrates a second pipeline."""
    run_id = config.get("training_run_id") or 0
    accuracy = round(0.74 + 0.001 * ((run_id + 1) % 25), 4)
    recall = round(0.82 + 0.001 * ((run_id + 1) % 20), 4)
    f1 = round(0.62 + 0.001 * ((run_id + 1) % 35), 4)

    tmpdir = tempfile.mkdtemp(prefix="churn-artifact-")
    artifact_path = os.path.join(tmpdir, "model.txt")
    with open(artifact_path, "w") as f:
        f.write(f"churn-balanced v1\naccuracy={accuracy}\nrecall={recall}\nf1={f1}\n")

    return {
        "status": "SUCCESS",
        "metrics": {"accuracy": accuracy, "recall": recall, "f1": f1},
        "artifact_path": artifact_path,
        "pipeline": "churn-balanced",
    }


def fail(config: dict) -> dict:
    raise RuntimeError("Customer Churn pipeline intentionally failed.")
