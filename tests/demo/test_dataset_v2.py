"""Dataset V2 = V1 + the drifted window, and the lineage that records it.

The property under test is that V2 *extends* V1 rather than replacing
it. A V2 that merely re-sampled the world at the new distribution would
pass a row-count check by accident, so the tests assert on content and
on the parent edge, not just on size.
"""

from __future__ import annotations

import csv
import json

import pytest

from case_studies.fraud_detection import data as fraud_data
from demo.steps import build_dataset_v2
from mlops_framework.database.models.dataset_version import DatasetVersion
from mlops_framework.dataset.manager import DatasetManager
from mlops_framework.exceptions import DatasetVersionNotFoundError
from mlops_framework.lineage.manager import LineageManager


def _rows(path):
    with path.open(newline="") as handle:
        return list(csv.reader(handle))


class TestComposition:
    def test_v2_row_count_is_v1_plus_the_window(self, ctx_drifted):
        v2_id = build_dataset_v2.run(ctx_drifted)
        with ctx_drifted.db.get_session() as session:
            v1 = session.get(DatasetVersion, ctx_drifted.v1_version_id)
            window = session.get(
                DatasetVersion, ctx_drifted.drifted_window_version_id
            )
            v2 = session.get(DatasetVersion, v2_id)
        assert v2.row_count == v1.row_count + window.row_count

    def test_v2_actually_contains_v1s_rows(self, ctx_drifted):
        """The point of the whole step: V2 must not have forgotten the
        population the model was already serving correctly."""
        cfg = ctx_drifted.config
        build_dataset_v2.run(ctx_drifted)

        v1_rows = _rows(cfg.local_path(cfg.v1_filename))
        v2_rows = _rows(cfg.local_path(cfg.v2_filename))
        # Header once, then V1 in order, then the window.
        assert v2_rows[0] == v1_rows[0]
        assert v2_rows[1 : len(v1_rows)] == v1_rows[1:]

    def test_v2_ends_with_the_drifted_window(self, ctx_drifted):
        cfg = ctx_drifted.config
        build_dataset_v2.run(ctx_drifted)

        window_rows = _rows(cfg.local_path(cfg.drifted_window_filename))
        v2_rows = _rows(cfg.local_path(cfg.v2_filename))
        assert v2_rows[-(len(window_rows) - 1) :] == window_rows[1:]

    def test_v2_is_not_a_regenerated_replacement_for_v1(self, ctx_drifted):
        """Guards the failure mode this step exists to prevent."""
        cfg = ctx_drifted.config
        build_dataset_v2.run(ctx_drifted)

        regenerated = fraud_data.write_csv(
            cfg.data_dir / "regenerated.csv",
            n_rows=cfg.n_rows,
            fraud_ratio=cfg.fraud_ratio,
            seed=cfg.seed,
            drift_shift=cfg.drifted_drift_shift,
        )
        v2 = cfg.local_path(cfg.v2_filename)
        assert v2.read_bytes() != regenerated.read_bytes()

    def test_the_schema_is_unchanged(self, ctx_drifted):
        """Only the statistics moved. A schema change would mean
        something else went wrong."""
        v2_id = build_dataset_v2.run(ctx_drifted)
        with ctx_drifted.db.get_session() as session:
            v1 = session.get(DatasetVersion, ctx_drifted.v1_version_id)
            v2 = session.get(DatasetVersion, v2_id)
        assert v2.schema_hash == v1.schema_hash


class TestLineage:
    def test_v2_records_v1_as_its_parent(self, ctx_drifted):
        v2_id = build_dataset_v2.run(ctx_drifted)
        with ctx_drifted.db.get_session() as session:
            v2 = session.get(DatasetVersion, v2_id)
        assert v2.parent_version_id == ctx_drifted.v1_version_id

    def test_the_derivation_recipe_is_recorded(self, ctx_drifted):
        v2_id = build_dataset_v2.run(ctx_drifted)
        with ctx_drifted.db.get_session() as session:
            meta = json.loads(session.get(DatasetVersion, v2_id).metadata_json)

        derivation = meta["derivation"]
        assert derivation["strategy"] == "append_production_window"
        assert derivation["parent_dataset_version_id"] == ctx_drifted.v1_version_id
        assert derivation["appended_dataset_version_id"] == (
            ctx_drifted.drifted_window_version_id
        )
        assert derivation["drift_event_id"] == ctx_drifted.state.drift_event_id

    def test_the_lineage_graph_exposes_the_derived_from_edge(self, ctx_drifted):
        """'Why does a model trained on V2 exist?' has to be answerable
        from the graph, not only from the demo's terminal output."""
        v2_id = build_dataset_v2.run(ctx_drifted)
        with ctx_drifted.db.get_session() as session:
            graph = LineageManager(session).graph_for_dataset_version(v2_id)

        edges = {(e.source, e.target, e.type) for e in graph.edges}
        assert (
            f"DatasetVersion:{ctx_drifted.v1_version_id}",
            f"DatasetVersion:{v2_id}",
            "derived_from",
        ) in edges

    def test_a_version_with_no_parent_adds_no_edge(self, ctx_with_v1):
        with ctx_with_v1.db.get_session() as session:
            graph = LineageManager(session).graph_for_dataset_version(
                ctx_with_v1.v1_version_id
            )
        assert not [e for e in graph.edges if e.type == "derived_from"]

    def test_a_parent_cycle_does_not_hang_the_walk(self, ctx_drifted):
        """Nothing in the framework can write one, but a hand-edited row
        must not take down the console."""
        v2_id = build_dataset_v2.run(ctx_drifted)
        with ctx_drifted.db.get_session() as session:
            v1 = session.get(DatasetVersion, ctx_drifted.v1_version_id)
            v1.parent_version_id = v2_id  # V1 -> V2 -> V1
            session.commit()
            graph = LineageManager(session).graph_for_dataset_version(v2_id)
        assert graph.nodes


class TestGuards:
    def test_an_unknown_parent_is_rejected_at_registration(self, ctx_with_v1):
        with ctx_with_v1.db.get_session() as session:
            dm = DatasetManager(session)
            with pytest.raises(DatasetVersionNotFoundError):
                dm.create_version(
                    dataset_id=ctx_with_v1.dataset_id,
                    storage_uri="/tmp/nope.csv",
                    row_count=1,
                    parent_version_id=9999,
                )

    def test_building_v2_twice_reuses_the_same_version(self, ctx_drifted):
        """Idempotence: re-running the step must not fork the lineage."""
        first = build_dataset_v2.run(ctx_drifted)
        second = build_dataset_v2.run(ctx_drifted)
        assert first == second

        with ctx_drifted.db.get_session() as session:
            versions = DatasetManager(session).list_versions(
                ctx_drifted.dataset_id
            )
        assert [v.id for v in versions].count(first) == 1
