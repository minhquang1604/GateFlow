"""Drift detection: the negative control, the positive case, the record.

The assertions that matter here are about *discrimination*. A detector
that flags the drifted window proves nothing on its own — one that flags
the drifted window and does not flag the baseline is making a claim.
"""

from __future__ import annotations

from case_studies.fraud_detection import data as fraud_data
from demo.steps import detect_drift, inject_drift, simulate_production
from mlops_framework.database.models.drift_evaluation import (
    DriftEvaluation,
    DriftOutcome,
)
from mlops_framework.database.models.governance_event import GovernanceEvent


class TestBaselineWindow:
    """The negative control."""

    def test_baseline_traffic_does_not_flag(self, ctx_with_v1):
        drifted = simulate_production.run(ctx_with_v1)
        assert drifted is False, (
            "the baseline window is drawn from the reference population; "
            "flagging it makes the later detection meaningless"
        )
        assert ctx_with_v1.state.drift_status == "NORMAL"

    def test_baseline_evaluation_is_persisted_as_no_drift(self, ctx_with_v1):
        simulate_production.run(ctx_with_v1)
        with ctx_with_v1.db.get_session() as session:
            rows = session.query(DriftEvaluation).all()
        assert len(rows) == 1
        assert rows[0].outcome == DriftOutcome.NO_DRIFT

    def test_baseline_raises_no_alert(self, ctx_with_v1):
        simulate_production.run(ctx_with_v1)
        with ctx_with_v1.db.get_session() as session:
            events = session.query(GovernanceEvent).filter_by(
                event_type="DRIFT_DETECTED"
            ).all()
        assert events == []


class TestMonitoredFeatures:
    def test_time_is_excluded_from_monitoring(self):
        """A row counter's distribution differs between any two windows,
        so monitoring it would flag drift every run."""
        assert "time" in fraud_data.feature_columns()
        assert "time" not in fraud_data.monitored_feature_columns()

    def test_every_other_feature_is_still_monitored(self):
        monitored = set(fraud_data.monitored_feature_columns())
        assert monitored == set(fraud_data.feature_columns()) - {"time"}


class TestDriftedWindow:
    """The positive case."""

    def test_shifted_traffic_is_flagged(self, ctx_with_v1):
        inject_drift.run(ctx_with_v1)
        result = detect_drift.run(ctx_with_v1)
        assert result.drift_detected is True
        assert ctx_with_v1.state.drift_status == "DRIFT_DETECTED"

    def test_the_flagged_features_are_the_injected_ones(self, ctx_with_v1):
        """The shift is targeted, and the detector should say so.

        Under the Bonferroni correction the flagged set should be a
        subset of the features the generator actually moved — anything
        else is a false positive that the correction exists to remove.
        """
        inject_drift.run(ctx_with_v1)
        result = detect_drift.run(ctx_with_v1)

        flagged = {f.feature for f in result.feature_results if f.drift_detected}
        injected = set(
            fraud_data.drift_parameters(
                drift_shift=ctx_with_v1.config.drifted_drift_shift,
                seed=ctx_with_v1.config.drifted_window_seed,
                n_rows=ctx_with_v1.config.window_rows,
                fraud_ratio=ctx_with_v1.config.fraud_ratio,
            )["affected_features"]
        )
        assert flagged, "expected the injected shift to be detected"
        assert flagged <= injected, (
            f"flagged features not in the injected set: {sorted(flagged - injected)}"
        )

    def test_the_correction_is_recorded_in_the_result(self, ctx_with_v1):
        """Reproducing a verdict needs the threshold actually applied."""
        inject_drift.run(ctx_with_v1)
        result = detect_drift.run(ctx_with_v1)
        assert "Bonferroni" in result.notes
        assert result.threshold < ctx_with_v1.config.drift_threshold


class TestDriftEventPersistence:
    def test_the_evaluation_names_both_versions_compared(self, ctx_with_v1):
        inject_drift.run(ctx_with_v1)
        detect_drift.run(ctx_with_v1)
        with ctx_with_v1.db.get_session() as session:
            row = (
                session.query(DriftEvaluation)
                .filter_by(outcome=DriftOutcome.DRIFT_DETECTED)
                .one()
            )
        assert row.reference_dataset_version_id == ctx_with_v1.v1_version_id
        assert row.current_dataset_version_id == ctx_with_v1.drifted_window_version_id

    def test_a_critical_governance_event_is_raised(self, ctx_with_v1):
        inject_drift.run(ctx_with_v1)
        detect_drift.run(ctx_with_v1)
        with ctx_with_v1.db.get_session() as session:
            events = session.query(GovernanceEvent).filter_by(
                event_type="DRIFT_DETECTED"
            ).all()
        assert len(events) == 1
        assert events[0].severity.value == "CRITICAL"
        assert events[0].entity_id == ctx_with_v1.drifted_window_version_id

    def test_the_event_id_is_threaded_into_demo_state(self, ctx_with_v1):
        inject_drift.run(ctx_with_v1)
        detect_drift.run(ctx_with_v1)
        assert ctx_with_v1.state.drift_event_id is not None
        assert ctx_with_v1.state.approval_status == "PENDING"


class TestNoiseInjectionIsReproducible:
    def test_the_generation_parameters_are_recorded_on_the_version(
        self, ctx_with_v1
    ):
        """The window must be regenerable from the audit trail alone."""
        import json

        from mlops_framework.database.models.dataset_version import DatasetVersion

        inject_drift.run(ctx_with_v1)
        with ctx_with_v1.db.get_session() as session:
            row = session.get(DatasetVersion, ctx_with_v1.drifted_window_version_id)
            meta = json.loads(row.metadata_json)

        generation = meta["generation"]
        assert generation["random_seed"] == ctx_with_v1.config.drifted_window_seed
        assert generation["parameters"]["drift_shift"] == (
            ctx_with_v1.config.drifted_drift_shift
        )
        assert generation["affected_features"]
        assert generation["affected_records"] > 0
        assert generation["generator"].endswith(":generate")

    def test_regenerating_from_the_recorded_seed_is_byte_identical(
        self, ctx_with_v1, tmp_path
    ):
        inject_drift.run(ctx_with_v1)
        original = ctx_with_v1.config.local_path(
            ctx_with_v1.config.drifted_window_filename
        )
        replay = fraud_data.write_csv(
            tmp_path / "replay.csv",
            n_rows=ctx_with_v1.config.window_rows,
            fraud_ratio=ctx_with_v1.config.fraud_ratio,
            seed=ctx_with_v1.config.drifted_window_seed,
            drift_shift=ctx_with_v1.config.drifted_drift_shift,
        )
        assert replay.read_bytes() == original.read_bytes()
