"""Reproducibility, and the wiring that carries it into each phase.

An experiment whose parameters depend on the machine it ran on is not
reproducible. These tests pin the two halves of that: the config is a
complete and self-contained description of a run, and the phases
actually use it rather than their own defaults.
"""

from __future__ import annotations

import dataclasses

import pytest

from demo.config import DemoConfig
from demo.context import DemoContext, DemoState
from demo.steps import initial_training, validate_model


class TestConfigIsSelfContained:
    def test_two_default_configs_are_identical(self):
        assert DemoConfig() == DemoConfig()

    def test_the_record_names_every_parameter_that_moves_a_result(self):
        record = DemoConfig().to_dict()
        for key in (
            "seed",
            "normal_window_seed",
            "drifted_window_seed",
            "drifted_drift_shift",
            "drift_threshold",
            "drift_correction",
            "training_params",
            "promotion_min_metrics",
            "required_rows",
            "n_rows",
            "window_rows",
            "fraud_ratio",
        ):
            assert key in record, f"{key} is missing from the reproducibility record"

    def test_from_env_does_not_let_the_environment_move_the_science(
        self, monkeypatch
    ):
        """Placement is environment-driven; seeds and thresholds are not."""
        monkeypatch.setenv("DEMO_DATA_DIR", "/tmp/elsewhere")
        monkeypatch.setenv("SEED", "999")
        monkeypatch.setenv("DRIFT_THRESHOLD", "0.5")

        config = DemoConfig.from_env()
        assert str(config.data_dir) == "/tmp/elsewhere"
        assert config.seed == DemoConfig().seed
        assert config.drift_threshold == DemoConfig().drift_threshold

    def test_explicit_overrides_still_win(self):
        config = DemoConfig.from_env(seed=7)
        assert config.seed == 7

    def test_the_config_is_frozen(self):
        """A phase must not be able to quietly retune the experiment."""
        with pytest.raises(dataclasses.FrozenInstanceError):
            DemoConfig().seed = 99  # type: ignore[misc]


class TestPathsSeparateLocalFromOrchestrator:
    """The demo writes a file; Airflow opens it under a different root."""

    def test_local_and_airflow_paths_share_a_filename(self):
        config = DemoConfig()
        assert config.local_path(config.v2_filename).name == config.v2_filename
        assert config.airflow_path(config.v2_filename).endswith(config.v2_filename)

    def test_the_airflow_path_is_not_the_local_one_by_default(self):
        config = DemoConfig()
        assert str(config.local_path(config.v1_filename)) != config.airflow_path(
            config.v1_filename
        )


class TestReadinessPolicyComesFromConfig:
    def test_the_policy_reflects_the_configured_row_bar(self):
        policy = DemoConfig(required_rows=42).training_policy()
        assert policy.required_size == 42

    def test_the_policy_requires_every_column_the_pipeline_reads(self):
        from case_studies.fraud_detection import data as fraud_data

        policy = DemoConfig().training_policy()
        expected = fraud_data.feature_columns() + [fraud_data.target_column()]
        assert policy.required_columns == expected
        assert policy.expected_column_count == len(expected)


class TestInitialTrainingWiring:
    """Phase 1 delegates, and must delegate the right things."""

    def test_it_passes_the_configured_seeds_and_paths(self, ctx, monkeypatch):
        captured = {}

        def _fake(db, endpoints, settings, **kwargs):
            captured.update(kwargs)
            raise SystemExit("stop after capture")

        monkeypatch.setattr(initial_training, "run_initial_training", _fake)
        with pytest.raises(SystemExit):
            initial_training.run(ctx)

        cfg = ctx.config
        assert captured["csv_write_kwargs"] == {
            "n_rows": cfg.n_rows,
            "fraud_ratio": cfg.fraud_ratio,
            "seed": cfg.seed,
            # V1 trains on the reference population by definition.
            "drift_shift": 0.0,
        }
        assert captured["training_params"] == cfg.training_params
        assert captured["csv_local_path"] == cfg.local_path(cfg.v1_filename)
        assert captured["airflow_csv_path"] == cfg.airflow_path(cfg.v1_filename)

    def test_it_passes_the_dag_id_and_the_entrypoint_separately(
        self, ctx, monkeypatch
    ):
        """Getting these backwards 404s against a DAG named after a
        Python module path."""
        captured = {}

        def _fake(db, endpoints, settings, **kwargs):
            captured.update(kwargs)
            raise SystemExit("stop")

        monkeypatch.setattr(initial_training, "run_initial_training", _fake)
        with pytest.raises(SystemExit):
            initial_training.run(ctx)

        assert captured["dag_id"] == ctx.config.dag_id
        assert captured["pipeline_id"] == ctx.config.pipeline_id


class TestValidationReporting:
    def test_a_missing_live_score_is_absent_not_invented(self, ctx_with_v1):
        """A gap in the evidence must not be filled with a number."""
        ctx_with_v1.v1_mlflow_run_id = None
        outcome = _StubOutcome(promoted=False, model_version_id=None)

        report = validate_model.run(ctx_with_v1, outcome)
        assert report["v1_live"] == {}

    def test_it_reports_the_stored_metrics_it_does_have(self, ctx_with_v1):
        ctx_with_v1.v1_mlflow_run_id = None
        report = validate_model.run(
            ctx_with_v1, _StubOutcome(promoted=False, model_version_id=None)
        )
        assert report["v1_stored"]["f1"] == 0.92


class TestEvidenceLog:
    def test_each_record_carries_the_lifecycle_coordinates(self):
        ctx = DemoContext(
            db=None, config=DemoConfig(), settings=None, endpoints={},
            state=DemoState(dataset_version="dataset_v1", model_version="model_v1"),
        )
        ctx.record("drift-monitor", "DRIFT_DETECTED", score=0.9)

        entry = ctx.evidence[0]
        assert entry["component"] == "drift-monitor"
        assert entry["event"] == "DRIFT_DETECTED"
        assert entry["dataset_version"] == "dataset_v1"
        assert entry["model_version"] == "model_v1"
        assert entry["score"] == 0.9
        assert entry["timestamp"]

    def test_a_missing_threaded_id_names_the_step_that_sets_it(self):
        ctx = DemoContext(db=None, config=DemoConfig(), settings=None, endpoints={})
        with pytest.raises(RuntimeError, match="v2_version_id"):
            ctx.require("v2_version_id")


@dataclasses.dataclass
class _StubOutcome:
    promoted: bool
    model_version_id: int | None
    steps: list = dataclasses.field(default_factory=list)
