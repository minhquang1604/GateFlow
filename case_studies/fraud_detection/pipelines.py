"""Pipelines used by the Fraud Detection case study.

Each ``main(config)`` is called by an orchestrator (Local or Airflow).
They never import from the framework — they only do work and return
a small dict that the orchestrator captures on stdout (or XCom, when
running inside Airflow).

Four pipelines are provided:

* :func:`train_baseline`     — a simple deterministic training pass.
                               Hermetic; no ML library imports.
* :func:`train_advanced`    — adds hand-engineered metrics.
                               Hermetic; no ML library imports.
* :func:`train_xgboost`     — REAL XGBoost on the fraud CSV. Used by
                               the production-side Airflow DAG. Logs
                               to MLflow when run inside a tracker.
* :func:`fail`               — used by tests to verify the SDK
                               surfaces :class:`TrainingError` on
                               pipeline failure.
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any


def train_baseline(config: dict) -> dict:
    """A trivial fraud-detection trainer.

    No ML library is used — this is a case study, not a real model.
    Returns a deterministic report.
    """
    run_id = config.get("training_run_id") or 0
    f1 = round(0.80 + 0.001 * ((run_id + 1) % 50), 4)
    roc = round(0.85 + 0.001 * ((run_id + 1) % 30), 4)

    tmpdir = tempfile.mkdtemp(prefix="fraud-artifact-")
    artifact_path = os.path.join(tmpdir, "model.txt")
    with open(artifact_path, "w") as f:
        f.write(f"fraud-baseline v1\nf1={f1}\nroc_auc={roc}\n")

    return {
        "status": "SUCCESS",
        "metrics": {"f1": f1, "roc_auc": roc},
        "artifact_path": artifact_path,
        "pipeline": "fraud-baseline",
    }


def train_advanced(config: dict) -> dict:
    """A second iteration of the fraud trainer.

    Same skeleton, slightly different numbers — demonstrates that a
    single SDK can drive multiple pipelines on the same dataset.
    """
    run_id = config.get("training_run_id") or 0
    f1 = round(0.86 + 0.001 * ((run_id + 1) % 40), 4)
    precision = round(0.90 + 0.001 * ((run_id + 1) % 25), 4)
    recall = round(0.78 + 0.001 * ((run_id + 1) % 20), 4)

    tmpdir = tempfile.mkdtemp(prefix="fraud-artifact-")
    artifact_path = os.path.join(tmpdir, "model.txt")
    with open(artifact_path, "w") as f:
        f.write(f"fraud-advanced v1\nf1={f1}\nprecision={precision}\nrecall={recall}\n")

    return {
        "status": "SUCCESS",
        "metrics": {"f1": f1, "precision": precision, "recall": recall},
        "artifact_path": artifact_path,
        "pipeline": "fraud-advanced",
    }


def fail(config: dict) -> dict:
    """Pipeline that always fails. Used in tests."""
    raise RuntimeError("Fraud Detection pipeline intentionally failed.")


def train_xgboost(config: dict) -> dict:
    """Real XGBoost trainer for the Fraud Detection case study.

    Reads ``csv_uri`` from ``config`` (the framework forwards
    ``dataset_version.storage_uri``), trains an
    :class:`xgboost.XGBClassifier` on the fraud CSV, and returns
    metrics + a serialized model artifact path.

    The pipeline additionally:

    * If a tracker run is provided via ``config["tracker_run_id"]``,
      logs params + metrics to MLflow under that run.
    * Always logs params + metrics via ``print`` so Airflow workers
      capture them in their logs even when MLflow is unavailable.

    Returns a dict shaped like the other pipelines (so Airflow/Local
    orchestrators capture it identically):

        {
            "status": "SUCCESS" | "FAILED",
            "metrics": {"f1": ..., "roc_auc": ..., ...},
            "artifact_path": "...",
            "params": {"max_depth": ..., "n_estimators": ...},
            "pipeline": "fraud-xgboost",
        }
    """
    # Imports are lazy so the module remains importable in environments
    # that don't have xgboost / sklearn / pandas installed.
    try:
        import numpy as np  # type: ignore[import-not-found]
        import pandas as pd  # type: ignore[import-not-found]
        import xgboost as xgb  # type: ignore[import-not-found]
        from sklearn.metrics import (  # type: ignore[import-not-found]
            average_precision_score,
            f1_score,
            precision_score,
            recall_score,
            roc_auc_score,
        )
        from sklearn.model_selection import train_test_split  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - env dependent
        return {
            "status": "FAILED",
            "error": f"train_xgboost requires xgboost, scikit-learn, pandas: {exc}",
            "pipeline": "fraud-xgboost",
        }

    params = {
        "max_depth": int(config.get("max_depth", 6)),
        "n_estimators": int(config.get("n_estimators", 200)),
        "learning_rate": float(config.get("learning_rate", 0.1)),
        "subsample": float(config.get("subsample", 0.9)),
        "colsample_bytree": float(config.get("colsample_bytree", 0.9)),
        "random_state": int(config.get("seed", 42)),
    }

    csv_uri = config.get("csv_uri")
    if not csv_uri:
        return {
            "status": "FAILED",
            "error": "train_xgboost requires 'csv_uri' in config",
            "pipeline": "fraud-xgboost",
        }

    # The real Kaggle file is Time,V1..V28,Amount,Class; the synthetic one
    # is time,amount,v1..v28,class. normalize_columns reconciles both and
    # raises if a column is genuinely absent, so the feature matrix can
    # never end up silently mis-aligned.
    from case_studies.fraud_detection.data import (
        feature_columns,
        normalize_columns,
        target_column,
    )

    try:
        df = normalize_columns(pd.read_csv(csv_uri))
    except ValueError as exc:
        return {
            "status": "FAILED",
            "error": f"{exc} (source: {csv_uri})",
            "pipeline": "fraud-xgboost",
        }
    feature_cols = feature_columns()
    target_col = target_column()

    X = df[feature_cols].to_numpy(dtype=np.float32)
    y = df[target_col].to_numpy(dtype=np.int32)

    # Stratified split keeps the fraud ratio stable in both partitions.
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, stratify=y, random_state=params["random_state"]
    )

    # scale_pos_weight balances the rare-positive class without
    # fabricating rows.
    n_pos = int((y_train == 1).sum())
    n_neg = int((y_train == 0).sum())
    if n_pos > 0:
        scale_pos_weight = float(n_neg / n_pos)
    else:
        scale_pos_weight = 1.0

    model = xgb.XGBClassifier(
        max_depth=params["max_depth"],
        n_estimators=params["n_estimators"],
        learning_rate=params["learning_rate"],
        subsample=params["subsample"],
        colsample_bytree=params["colsample_bytree"],
        scale_pos_weight=scale_pos_weight,
        eval_metric="logloss",
        n_jobs=1,
        random_state=params["random_state"],
    )
    model.fit(X_train, y_train)

    y_pred = model.predict(X_test)
    y_proba = model.predict_proba(X_test)[:, 1]
    metrics = {
        "f1": float(f1_score(y_test, y_pred, zero_division=0)),
        "precision": float(precision_score(y_test, y_pred, zero_division=0)),
        "recall": float(recall_score(y_test, y_pred, zero_division=0)),
        "roc_auc": float(roc_auc_score(y_test, y_proba)) if n_pos > 0 else 0.0,
        # Average precision (area under the precision-recall curve) is the
        # metric that matters on this dataset: at a 0.17% positive rate a
        # model that never predicts fraud still scores ~0.95 ROC-AUC, while
        # its average precision stays near the base rate. The promotion
        # policy should gate on this, not on roc_auc.
        "average_precision": (
            float(average_precision_score(y_test, y_proba)) if n_pos > 0 else 0.0
        ),
        "scale_pos_weight": scale_pos_weight,
    }
    params.update(
        {
            "n_rows": int(len(df)),
            "n_features": len(feature_cols),
            "n_fraud_train": n_pos,
            "n_fraud_test": int((y_test == 1).sum()),
        }
    )

    tmpdir = tempfile.mkdtemp(prefix="fraud-xgb-artifact-")
    artifact_path = os.path.join(tmpdir, "model.json")
    model.save_model(artifact_path)

    # Optional MLflow logging. We don't import mlflow at module level
    # so the case study still works when MLflow is unavailable.
    tracker_run_id = config.get("tracker_run_id")
    if tracker_run_id:
        try:
            import mlflow  # type: ignore[import-not-found]

            tracking_uri = config.get("tracking_uri")
            if tracking_uri:
                mlflow.set_tracking_uri(tracking_uri)
            with mlflow.start_run(run_id=tracker_run_id):
                mlflow.log_params(params)
                mlflow.log_metrics(metrics)
                mlflow.log_artifact(artifact_path)
        except Exception as exc:  # pragma: no cover - env dependent
            print(f"[fraud-xgboost] mlflow logging skipped: {exc}")

    # Always emit a single-line JSON summary for the orchestrator.
    print(json.dumps({"params": params, "metrics": metrics}))

    return {
        "status": "SUCCESS",
        "metrics": metrics,
        "artifact_path": artifact_path,
        "params": params,
        "pipeline": "fraud-xgboost",
    }