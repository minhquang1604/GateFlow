"""Every knob the closed-loop demo turns, in one place.

Nothing in ``demo/`` reads an environment variable or hard-codes a
threshold outside this module. That is the point: a reader who wants to
know what the experiment actually did, or a reviewer who wants to
reproduce it, has one file to read rather than nine steps to audit.

The values below are calibrated so the story holds: V1 self-evaluates
around F1 0.92, V1 scored on the drifted window collapses to roughly
0.46-0.50, and V2 retrained on V1 + that window recovers to about
0.80-0.85. Changing ``drift_shift`` or ``n_rows`` moves all three.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class DemoConfig:
    """A complete, reproducible description of one demo run."""

    # -- Identity ----------------------------------------------------- #
    #: The dataset the model trains on. Holds V1 and, after a retrain, V2.
    dataset_name: str = "credit-card-fraud"
    #: Traffic the deployed model actually scored. A *separate* dataset,
    #: deliberately: a production window is an observation of the world,
    #: not a version of the training set, and collapsing the two is what
    #: makes "which data drifted?" unanswerable later.
    production_dataset_name: str = "credit-card-fraud-production"
    model_name: str = "fraud-xgboost"

    #: What LocalDockerOrchestrator would import, and what the Airflow
    #: DAG imports after reading it back over HTTP. Not the DAG id.
    pipeline_id: str = "case_studies.fraud_detection.pipelines:train_xgboost"
    #: What AirflowOrchestrator triggers. See the README's
    #: "AirflowOrchestrator vs LocalDockerOrchestrator".
    dag_id: str = "mlops_training_pipeline"
    experiment_name: str = "fraud-closed-loop"

    # -- Dataset V1 --------------------------------------------------- #
    n_rows: int = 8000
    fraud_ratio: float = 0.02
    seed: int = 42

    # -- Production windows ------------------------------------------- #
    #: Seeds differ from ``seed`` so a window is genuinely fresh traffic
    #: rather than a replay of the training rows; they are fixed so the
    #: run reproduces exactly.
    window_rows: int = 1000
    normal_window_seed: int = 1001
    drifted_window_seed: int = 2002
    #: 0.0 — the baseline window is drawn from the reference population.
    #: Establishing that the detector says NO DRIFT here is what makes
    #: its later verdict evidence rather than a foregone conclusion.
    normal_drift_shift: float = 0.0
    drifted_drift_shift: float = 1.0

    # -- Detection ---------------------------------------------------- #
    #: Family-wise p-value threshold for the KS / chi-square tests.
    drift_threshold: float = 0.05
    #: Bonferroni, not the framework default of "none". 29 features are
    #: compared per window; testing each at 0.05 and declaring drift if
    #: *any* is significant gives a false-positive rate near 79%, so the
    #: baseline window would be flagged in most runs and the negative
    #: control — the thing that makes the real detection evidence —
    #: would be worthless. See DriftConfig.correction.
    drift_correction: str = "bonferroni"

    # -- Training ----------------------------------------------------- #
    training_params: dict[str, Any] = field(
        default_factory=lambda: {
            "max_depth": 6,
            "n_estimators": 200,
            "learning_rate": 0.1,
        }
    )

    # -- Promotion / validation --------------------------------------- #
    #: An absolute quality floor rather than "must beat production".
    #: Once the population has shifted, V1's *stored* training-time
    #: metric was measured on a population that no longer exists, so
    #: comparing V2 against it is not a fair bar in either direction —
    #: see docs/specs and the validation step's own report, which shows
    #: V1 re-scored on the drifted window alongside it.
    promotion_min_metrics: dict[str, float] = field(
        default_factory=lambda: {"f1": 0.70, "precision": 0.70}
    )
    must_beat_production: bool = False
    #: V1 has no predecessor, so its own promotion is a cold start.
    allow_cold_start: bool = True
    #: The floor V1 itself must clear to reach PRODUCTION.
    v1_min_f1: float = 0.5

    # -- Readiness ---------------------------------------------------- #
    #: Minimum rows a dataset version needs before it may be trained on.
    #: Explicit here rather than buried in a default so that a run at a
    #: different scale — a test, a smaller replication — states its own
    #: bar instead of silently failing someone else's.
    required_rows: int = 1000
    #: Effectively unbounded: the demo's datasets are generated during
    #: the run, so freshness is never the interesting constraint.
    freshness_hours: int = 24 * 365
    max_missing_ratio: float = 0.0

    # -- Execution ---------------------------------------------------- #
    #: Where this process writes generated CSVs.
    data_dir: Path = REPO_ROOT / "demo" / "data"
    #: The same files as the Airflow containers see them. Differs from
    #: ``data_dir`` whenever the demo runs on the host rather than in the
    #: ``demo`` container — the bind mount lands at /opt/demo_data inside
    #: Airflow either way (see docker-compose.yml).
    airflow_data_dir: str = "/opt/demo_data"
    #: Generous: a full DAG run is scheduling latency across six tasks on
    #: top of the training itself.
    dag_timeout: float = 600.0
    approval_timeout: float = 3600.0

    # ------------------------------------------------------------------ #

    def training_policy(self):
        """The readiness policy every phase of this run is judged against."""
        from case_studies.fraud_detection import data as fraud_data
        from mlops_framework.readiness.engine import TrainingPolicy

        columns = fraud_data.feature_columns() + [fraud_data.target_column()]
        return TrainingPolicy(
            required_size=self.required_rows,
            freshness_hours=self.freshness_hours,
            required_columns=columns,
            expected_column_count=len(columns),
            max_missing_ratio=self.max_missing_ratio,
        )

    @classmethod
    def from_env(cls, **overrides: Any) -> DemoConfig:
        """Build a config, letting the environment override placement.

        Only the *location* knobs are environment-driven. Seeds,
        thresholds and hyperparameters deliberately are not: an
        experiment whose parameters depend on the shell it ran in is not
        reproducible, and silently picking up a stray env var is exactly
        how that happens.
        """
        env_overrides: dict[str, Any] = {}
        if data_dir := os.environ.get("DEMO_DATA_DIR"):
            env_overrides["data_dir"] = Path(data_dir)
        if airflow_dir := os.environ.get("DEMO_AIRFLOW_DATA_DIR"):
            env_overrides["airflow_data_dir"] = airflow_dir
        if dag_id := os.environ.get("AIRFLOW_DAG_ID"):
            env_overrides["dag_id"] = dag_id
        return replace(cls(), **{**env_overrides, **overrides})

    # -- Derived paths -------------------------------------------------- #
    # Two forms of every path: the one this process opens, and the one
    # recorded as the DatasetVersion's storage_uri for Airflow to open.

    def local_path(self, name: str) -> Path:
        return self.data_dir / name

    def airflow_path(self, name: str) -> str:
        return f"{self.airflow_data_dir.rstrip('/')}/{name}"

    @property
    def v1_filename(self) -> str:
        return "dataset_v1.csv"

    @property
    def v2_filename(self) -> str:
        return "dataset_v2.csv"

    @property
    def normal_window_filename(self) -> str:
        return "production_window_normal.csv"

    @property
    def drifted_window_filename(self) -> str:
        return "production_window_drifted.csv"

    def to_dict(self) -> dict[str, Any]:
        """The reproducibility record printed at the start of every run."""
        return {
            "dataset_name": self.dataset_name,
            "production_dataset_name": self.production_dataset_name,
            "model_name": self.model_name,
            "pipeline_id": self.pipeline_id,
            "dag_id": self.dag_id,
            "experiment_name": self.experiment_name,
            "n_rows": self.n_rows,
            "fraud_ratio": self.fraud_ratio,
            "seed": self.seed,
            "window_rows": self.window_rows,
            "normal_window_seed": self.normal_window_seed,
            "drifted_window_seed": self.drifted_window_seed,
            "normal_drift_shift": self.normal_drift_shift,
            "drifted_drift_shift": self.drifted_drift_shift,
            "drift_threshold": self.drift_threshold,
            "drift_correction": self.drift_correction,
            "training_params": dict(self.training_params),
            "promotion_min_metrics": dict(self.promotion_min_metrics),
            "must_beat_production": self.must_beat_production,
            "v1_min_f1": self.v1_min_f1,
            "required_rows": self.required_rows,
            "data_dir": str(self.data_dir),
            "airflow_data_dir": self.airflow_data_dir,
        }
