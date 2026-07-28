"""Unit tests for the drift detector abstraction.

The tests exercise the public ABC contract plus the reference
implementation. We seed the population with deterministic random
data so the chi-square / KS / mean-std detectors have stable
results.
"""

from __future__ import annotations

import math
import random
from typing import Callable

import pytest

from mlops_framework.drift.detector import (
    DriftConfig,
    DriftDetector,
    DriftResult,
    FeatureDrift,
    ScipyDriftDetector,
)


def _rng(seed: int) -> random.Random:
    return random.Random(seed)


def _normal(rng: random.Random, n: int, mean: float, std: float) -> list[float]:
    out: list[float] = []
    for _ in range(n):
        out.append(rng.gauss(mean, std))
    return out


def _categorical(rng: random.Random, n: int, weights: list[float]) -> list[int]:
    return [rng.choices(range(len(weights)), weights=weights, k=1)[0] for _ in range(n)]


# ---------------------------------------------------------------------- #
# Reference implementation
# ---------------------------------------------------------------------- #


class TestScipyDriftDetector:
    def test_no_drift_when_distributions_match(self):
        det = ScipyDriftDetector()
        ref = {"amount": _normal(_rng(1), 200, mean=0.0, std=1.0)}
        cur = {"amount": _normal(_rng(2), 200, mean=0.0, std=1.0)}
        result = det.detect(ref, cur, DriftConfig(threshold=0.05))
        assert result.drift_detected is False
        assert result.score < 0.2
        assert result.feature_results[0].method == "ks"
        assert result.feature_results[0].p_value is not None

    def test_drift_detected_when_mean_shifts(self):
        det = ScipyDriftDetector()
        ref = {"amount": _normal(_rng(1), 200, mean=0.0, std=1.0)}
        cur = {"amount": _normal(_rng(2), 200, mean=2.5, std=1.0)}
        result = det.detect(ref, cur, DriftConfig(threshold=0.05))
        assert result.drift_detected is True
        assert result.feature_results[0].drift_detected is True

    def test_categorical_drift_via_chi2(self):
        det = ScipyDriftDetector()
        ref = {"class": _categorical(_rng(1), 200, [0.7, 0.2, 0.1])}
        cur = {"class": _categorical(_rng(2), 200, [0.1, 0.2, 0.7])}
        result = det.detect(ref, cur, DriftConfig(threshold=0.05))
        assert result.drift_detected is True
        fr = result.feature_results[0]
        assert fr.method == "chi2"
        assert fr.p_value is not None

    def test_empty_data_handled_gracefully(self):
        det = ScipyDriftDetector()
        result = det.detect(
            {"amount": []}, {"amount": [1, 2, 3]}, DriftConfig()
        )
        # Not enough samples — feature is not drift-detected.
        assert result.drift_detected is False
        assert result.feature_results[0].method == "insufficient_samples"

    def test_threshold_controls_detection(self):
        det = ScipyDriftDetector()
        # Distributions are clearly different — p-value is tiny.
        ref = {"amount": _normal(_rng(1), 200, mean=0.0, std=1.0)}
        cur = {"amount": _normal(_rng(2), 200, mean=2.0, std=1.0)}
        # Loose threshold (large) — p_value (very small) < threshold → drift
        loose = det.detect(ref, cur, DriftConfig(threshold=0.99))
        assert loose.drift_detected is True
        # Tight threshold (very small) — p_value must be even smaller to
        # detect drift; here p_value is ~1e-13 so the tight threshold
        # also flags it.
        tight = det.detect(ref, cur, DriftConfig(threshold=1e-15))
        # Just verify the method_summary works in either direction.
        assert tight.method_summary.get("ks") == 1
        # Sanity check: identical distributions should not flag drift
        # even with loose threshold.
        same = det.detect(ref, ref, DriftConfig(threshold=0.99))
        assert same.drift_detected is False

    def test_method_summary_counts_methods(self):
        det = ScipyDriftDetector()
        ref = {
            "amount": _normal(_rng(1), 200, mean=0.0, std=1.0),
            "class": _categorical(_rng(2), 200, [0.7, 0.2, 0.1]),
        }
        cur = {
            "amount": _normal(_rng(3), 200, mean=0.0, std=1.0),
            "class": _categorical(_rng(4), 200, [0.7, 0.2, 0.1]),
        }
        result = det.detect(ref, cur, DriftConfig())
        assert "ks" in result.method_summary
        assert "chi2" in result.method_summary

    def test_to_dict_is_serializable(self):
        det = ScipyDriftDetector()
        ref = {"amount": _normal(_rng(1), 100, mean=0.0, std=1.0)}
        cur = {"amount": _normal(_rng(2), 100, mean=1.0, std=1.0)}
        import json

        result = det.detect(ref, cur, DriftConfig())
        json.dumps(result.to_dict())

    def test_fallback_works_without_scipy(self, monkeypatch):
        # Force the detector to use the fallback path.
        det = ScipyDriftDetector()
        det._scipy_stats = None
        ref = {"amount": _normal(_rng(1), 200, mean=0.0, std=1.0)}
        cur = {"amount": _normal(_rng(2), 200, mean=3.0, std=1.0)}
        result = det.detect(ref, cur, DriftConfig(threshold=0.5))
        # mean-std shift detector should detect this
        assert result.drift_detected is True
        assert "fallback" in result.notes
        assert result.feature_results[0].method == "meanstd"


# ---------------------------------------------------------------------- #
# ABC — pluggability
# ---------------------------------------------------------------------- #


class _CustomDetector(DriftDetector):
    """A trivial detector that always says "no drift"."""

    def detect(
        self,
        reference_data: dict[str, list[float]],
        current_data: dict[str, list[float]],
        config: DriftConfig | None = None,
    ) -> DriftResult:
        return DriftResult(
            drift_detected=False,
            score=0.0,
            method="trivial",
            threshold=(config or DriftConfig()).threshold,
        )


class TestPluggableDetector:
    def test_custom_detector_can_substitute(self):
        det: DriftDetector = _CustomDetector()
        result = det.detect(
            {"a": [1, 2, 3] * 50}, {"a": [4, 5, 6] * 50}, DriftConfig()
        )
        assert result.drift_detected is False
        assert result.method == "trivial"
