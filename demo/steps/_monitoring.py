"""The monitoring pass, shared by the baseline and the drifted window.

Both Phase 3 (normal traffic) and Phase 5 (shifted traffic) run the
*same* code against the *same* reference and the *same* threshold. That
is deliberate and it is the point: a detector only tested on data it was
built to flag proves nothing. The baseline pass exists so the later
verdict is evidence rather than a foregone conclusion, and nothing about
the drifted pass may take a different path to get there.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from case_studies.fraud_detection import data as fraud_data
from demo.context import DemoContext
from demo.reporting import drift_report, kv
from mlops_framework.database.models.dataset_version import DatasetVersion
from mlops_framework.dataset.checksum import calculate_file_checksum
from mlops_framework.dataset.manager import DatasetManager
from mlops_framework.drift.detector import (
    DriftConfig,
    DriftService,
    ScipyDriftDetector,
)


def feature_frame(csv_path: str | Path) -> dict[str, list[float]]:
    """Load a fraud CSV as ``{feature: [values]}``.

    Every *monitored* column, not only the ones the generator shifts —
    the unshifted features are what let the KS test demonstrate a
    *targeted* covariate shift rather than a wholesale change of file.
    ``time`` is excluded; see ``monitored_feature_columns`` for why
    monitoring a row counter guarantees a false positive.
    """
    import pandas as pd

    df = fraud_data.normalize_columns(pd.read_csv(csv_path))
    return {
        col: df[col].astype(float).tolist()
        for col in fraud_data.monitored_feature_columns()
    }


def ensure_production_dataset(ctx: DemoContext) -> int:
    """The logical dataset that holds observed production traffic.

    Separate from the training dataset on purpose — see
    ``DemoConfig.production_dataset_name``.
    """
    if ctx.production_dataset_id is not None:
        return ctx.production_dataset_id
    with ctx.db.get_session() as session:
        dm = DatasetManager(session)
        dataset = dm.get_dataset_by_name(ctx.config.production_dataset_name)
        if dataset is None:
            dataset = dm.create_dataset(
                ctx.config.production_dataset_name,
                description=(
                    "Inference traffic observed in production. Windows are "
                    "registered here as they are scored; drift is measured "
                    "against the training dataset's reference version."
                ),
            )
        session.commit()
        ctx.production_dataset_id = dataset.id
    return ctx.production_dataset_id


def register_window(
    ctx: DemoContext,
    *,
    filename: str,
    seed: float,
    drift_shift: float,
    label: str,
    extra_metadata: dict[str, Any] | None = None,
) -> tuple[int, Path]:
    """Write a production window and register it as a DatasetVersion.

    A window is registered — not just written — so the drift evaluation
    that references it has a real, immutable, checksummed row to point
    at. An evaluation whose subject cannot be identified afterwards is
    not auditable.
    """
    cfg = ctx.config
    dataset_id = ensure_production_dataset(ctx)
    local = cfg.local_path(filename)

    fraud_data.write_csv(
        local,
        n_rows=cfg.window_rows,
        fraud_ratio=cfg.fraud_ratio,
        seed=int(seed),
        drift_shift=drift_shift,
    )
    profile = fraud_data.describe_csv(local)
    metadata = dict(profile["metadata"])
    metadata["content_sha256"] = calculate_file_checksum(local)
    metadata["size_bytes"] = local.stat().st_size
    metadata["window_label"] = label
    metadata["generation"] = fraud_data.drift_parameters(
        drift_shift=drift_shift,
        seed=int(seed),
        n_rows=cfg.window_rows,
        fraud_ratio=cfg.fraud_ratio,
    )
    if extra_metadata:
        metadata.update(extra_metadata)

    storage_uri = cfg.airflow_path(filename)
    with ctx.db.get_session() as session:
        dm = DatasetManager(session)
        existing = [
            v for v in dm.list_versions(dataset_id) if v.storage_uri == storage_uri
        ]
        if existing:
            version = existing[-1]
        else:
            version = dm.create_version(
                dataset_id=dataset_id,
                storage_uri=storage_uri,
                row_count=profile["row_count"],
                metadata=metadata,
            )
        session.commit()
        version_id = version.id
        version_number = version.version_number

    kv("Window", f"{label} (production v{version_number}, id={version_id})", width=20)
    kv("Rows", f"{profile['row_count']:,}", width=20)
    kv("Fraud rows", f"{metadata['n_fraud']} ({metadata['fraud_ratio']:.4%})", width=20)
    kv("Content sha256", f"{metadata['content_sha256'][:16]}...", width=20)
    return version_id, local


def monitor(
    ctx: DemoContext,
    *,
    window_version_id: int,
    window_path: Path,
    window_label: str,
    notes: str,
) -> Any:
    """Compare one production window against the reference version.

    Reference is dataset V1 — the population the production model was
    fit on — not the previous window. Comparing consecutive windows
    would measure how fast traffic is changing; comparing against the
    training reference measures whether the deployed model's assumptions
    still hold, which is the question that should trigger a retrain.
    """
    cfg = ctx.config
    v1_id = ctx.require("v1_version_id")
    reference = feature_frame(cfg.local_path(cfg.v1_filename))
    current = feature_frame(window_path)

    with ctx.db.get_session() as session:
        reference_version = session.get(DatasetVersion, v1_id)
        window_version = session.get(DatasetVersion, window_version_id)
        service = DriftService(session, ScipyDriftDetector())
        result = service.evaluate(
            reference_version=reference_version,
            current_version=window_version,
            reference_data=reference,
            current_data=current,
            config=DriftConfig(
                threshold=cfg.drift_threshold,
                correction=cfg.drift_correction,
            ),
            notes=notes,
        )
        reference_label = (
            f"dataset_v{reference_version.version_number} "
            f"(id={reference_version.id})"
        )
        session.commit()

    sample_feature = next(iter(reference.values()), [])
    sample_current = next(iter(current.values()), [])
    drift_report(
        reference_label=reference_label,
        production_label=f"{window_label} (id={window_version_id})",
        reference_samples=len(sample_feature),
        production_samples=len(sample_current),
        feature_results=result.feature_results,
        score=result.score,
        threshold=result.threshold,
        detected=result.drift_detected,
        notes=result.notes,
    )
    return result
