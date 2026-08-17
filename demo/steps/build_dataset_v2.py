"""Phase 9 — construct dataset V2 = V1 + the drifted production window.

The requirement that makes this a *learning* loop rather than a
restart: V2 extends V1, it does not replace it.

Training only on the drifted window would produce a model that handles
today's traffic and has forgotten the population it was already serving
correctly — catastrophic forgetting dressed up as a fix. Regenerating V1
at the new distribution (which the previous demo did) is worse still: it
quietly rewrites history so the "before" population never existed, and
the lineage then claims the model was trained on data that was never
observed.

So V2 is a concatenation, in order, of exactly two files that both
already exist on disk and are both already registered, checksummed and
immutable. Its row count is the sum of theirs, and
``parent_version_id`` points at V1 — the edge that lets
:class:`~mlops_framework.lineage.manager.LineageManager` answer why a
model trained on V2 exists.

This runs *after* approval. Building it earlier would mean doing work
the admin might reject.
"""

from __future__ import annotations

from case_studies.fraud_detection import data as fraud_data
from demo.context import DemoContext
from demo.reporting import banner, bullet, detail, kv, section
from mlops_framework.database.models.dataset_version import DatasetVersion
from mlops_framework.dataset.checksum import calculate_file_checksum
from mlops_framework.dataset.manager import DatasetManager


def run(ctx: DemoContext) -> int:
    """Build and register V2. Returns its DatasetVersion id."""
    cfg = ctx.config
    v1_id = ctx.require("v1_version_id")
    window_id = ctx.require("drifted_window_version_id")

    banner("DATASET V2 CONSTRUCTION")

    v1_local = cfg.local_path(cfg.v1_filename)
    window_local = cfg.local_path(cfg.drifted_window_filename)
    v2_local = cfg.local_path(cfg.v2_filename)

    section("Sources")
    kv("Parent (V1)", f"{v1_local.name} (version id={v1_id})", width=22)
    kv("New data", f"{window_local.name} (window id={window_id})", width=22)

    fraud_data.concat_csv([v1_local, window_local], v2_local)

    profile = fraud_data.describe_csv(v2_local)
    content_sha256 = calculate_file_checksum(v2_local)

    with ctx.db.get_session() as session:
        dm = DatasetManager(session)
        v1_row = session.get(DatasetVersion, v1_id)
        window_row = session.get(DatasetVersion, window_id)
        expected_rows = v1_row.row_count + window_row.row_count

        if profile["row_count"] != expected_rows:
            raise RuntimeError(
                f"dataset V2 has {profile['row_count']:,} rows but V1 "
                f"({v1_row.row_count:,}) + window ({window_row.row_count:,}) "
                f"= {expected_rows:,}. Refusing to register a version whose "
                f"contents do not match its declared lineage."
            )

        metadata = dict(profile["metadata"])
        metadata["content_sha256"] = content_sha256
        metadata["size_bytes"] = v2_local.stat().st_size
        # The construction recipe, recorded on the version itself, so V2
        # can be rebuilt from the registry without reading this file.
        metadata["derivation"] = {
            "strategy": "append_production_window",
            "parent_dataset_version_id": v1_id,
            "parent_row_count": v1_row.row_count,
            "parent_content_sha256": _content_hash(v1_row),
            "appended_dataset_version_id": window_id,
            "appended_row_count": window_row.row_count,
            "appended_content_sha256": _content_hash(window_row),
            "drift_event_id": ctx.state.drift_event_id,
            "builder": "case_studies.fraud_detection.data:concat_csv",
        }

        storage_uri = cfg.airflow_path(cfg.v2_filename)
        existing = [
            v
            for v in dm.list_versions(ctx.require("dataset_id"))
            if v.storage_uri == storage_uri
        ]
        if existing:
            version = existing[-1]
            detail(f"dataset version {storage_uri} already registered — reusing")
        else:
            version = dm.create_version(
                dataset_id=ctx.require("dataset_id"),
                storage_uri=storage_uri,
                row_count=profile["row_count"],
                metadata=metadata,
                parent_version_id=v1_id,
            )
        session.commit()
        version_id = version.id
        version_number = version.version_number
        parent_id = version.parent_version_id
        schema_hash = version.schema_hash
        v1_schema_hash = v1_row.schema_hash

    ctx.v2_version_id = version_id
    ctx.state.dataset_version = f"dataset_v{version_number}"
    ctx.state.dataset_version_id = version_id

    section("Dataset V2 registered")
    kv("Version", f"dataset_v{version_number} (id={version_id})", width=22)
    kv("Parent version", f"id={parent_id}", width=22)
    kv("Rows", f"{profile['row_count']:,}", width=22)
    kv(
        "Composition",
        f"{v1_row_count(ctx, v1_id):,} from V1 + {cfg.window_rows:,} observed",
        width=22,
    )
    kv("Fraud rows", f"{metadata['n_fraud']} ({metadata['fraud_ratio']:.4%})", width=22)
    kv("Content sha256", f"{content_sha256[:16]}...", width=22)
    kv("Schema hash", f"{schema_hash[:16]}...", width=22)
    kv(
        "Schema vs V1",
        "unchanged" if schema_hash == v1_schema_hash else "CHANGED",
        width=22,
    )

    detail("")
    detail("Lineage recorded:")
    bullet(f"dataset_v{version_number} derived_from dataset version #{v1_id}")
    bullet(f"extended with production window #{window_id}")
    bullet(f"because drift_event_{ctx.state.drift_event_id} was approved")

    ctx.record(
        "dataset-builder",
        "DATASET_VERSION_CREATED",
        dataset_version_id=version_id,
        parent_version_id=parent_id,
        row_count=profile["row_count"],
        appended_window_version_id=window_id,
        status="REGISTERED",
    )
    return version_id


def v1_row_count(ctx: DemoContext, v1_id: int) -> int:
    with ctx.db.get_session() as session:
        row = session.get(DatasetVersion, v1_id)
        return row.row_count if row is not None else 0


def _content_hash(version: DatasetVersion) -> str | None:
    import json

    try:
        return json.loads(version.metadata_json or "{}").get("content_sha256")
    except (ValueError, TypeError):
        return None
