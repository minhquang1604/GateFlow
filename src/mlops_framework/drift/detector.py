"""Drift detection abstraction and reference implementation.

The framework does not depend on a specific statistical library. The
:class:`DriftDetector` ABC is the only contract callers rely on, and
results are normalized into a :class:`DriftResult`. The reference
implementation :class:`ScipyDriftDetector` uses ``scipy.stats`` for
Kolmogorov–Smirnov (numerical) and chi-square (categorical) tests.

External library
----------------

The default implementation imports :mod:`scipy.stats` lazily. If
scipy is not installed, the engine falls back to a simple
distribution-statistics based detector (mean/std shift) so the
framework remains runnable in minimal environments.
"""

from __future__ import annotations

import json
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from mlops_framework.database.models.dataset_version import DatasetVersion
from mlops_framework.database.models.drift_evaluation import (
    DriftEvaluation,
    DriftOutcome,
)


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------- #
# Data classes
# ---------------------------------------------------------------------- #


@dataclass
class DriftConfig:
    """Per-detector configuration.

    The same dataclass is used by every detector; the meaning of
    fields varies slightly. ``threshold`` is the per-feature p-value
    threshold for KS and chi-square tests. For numeric detectors, the
    fallback implementation treats ``threshold`` as the standard
    deviation difference that triggers drift.
    """

    threshold: float = 0.05
    min_samples: int = 30
    methods: list[str] = field(default_factory=lambda: ["ks", "chi2"])
    #: How to correct ``threshold`` for testing many features at once.
    #:
    #: ``"none"`` tests every feature at ``threshold`` and declares drift
    #: if any one of them is significant. That is the historical
    #: behaviour and remains the default, but it is only sound for a
    #: handful of features: across *n* independent tests at alpha=0.05
    #: the probability of at least one false positive is 1-0.95**n, which
    #: is 40% at ten features and 79% at thirty. A monitor that cries
    #: drift four runs in five is worse than no monitor, because the
    #: response to it is to stop believing it.
    #:
    #: ``"bonferroni"`` divides the threshold by the number of features
    #: actually tested, holding the *family-wise* error rate at
    #: ``threshold``. Conservative — it will miss shifts too small to
    #: survive the division — which is the right trade for a signal whose
    #: job is to interrupt a human and authorise spending compute.
    correction: str = "none"

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> DriftConfig:
        if data is None:
            return cls()
        return cls(
            threshold=float(data.get("threshold", 0.05)),
            min_samples=int(data.get("min_samples", 30)),
            methods=list(data.get("methods") or ["ks", "chi2"]),
            correction=str(data.get("correction", "none")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "threshold": self.threshold,
            "min_samples": self.min_samples,
            "methods": list(self.methods),
            "correction": self.correction,
        }


@dataclass
class FeatureDrift:
    """Drift assessment for a single feature."""

    feature: str
    method: str
    score: float
    drift_detected: bool
    p_value: float | None = None
    detail: str = ""


@dataclass
class DriftResult:
    """Normalized, framework-level drift verdict."""

    drift_detected: bool
    score: float
    method: str
    threshold: float
    feature_results: list[FeatureDrift] = field(default_factory=list)
    method_summary: dict[str, int] = field(default_factory=dict)
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "drift_detected": self.drift_detected,
            "score": self.score,
            "method": self.method,
            "threshold": self.threshold,
            "feature_results": [
                {
                    "feature": f.feature,
                    "method": f.method,
                    "score": f.score,
                    "drift_detected": f.drift_detected,
                    "p_value": f.p_value,
                    "detail": f.detail,
                }
                for f in self.feature_results
            ],
            "method_summary": dict(self.method_summary),
            "notes": self.notes,
        }


# ---------------------------------------------------------------------- #
# ABC
# ---------------------------------------------------------------------- #


class DriftDetector(ABC):
    """Abstract drift detector.

    Implementations may use scipy, evidently, an HTTP-based detector,
    or any other backing. The framework only requires
    :meth:`detect` to return a normalized :class:`DriftResult`.
    """

    @abstractmethod
    def detect(
        self,
        reference_data: dict[str, list[float]],
        current_data: dict[str, list[float]],
        config: DriftConfig | None = None,
    ) -> DriftResult:
        """Compare ``current_data`` against ``reference_data``.

        Args:
            reference_data: Mapping of feature name -> list of
                observed values (training reference distribution).
            current_data: Mapping of feature name -> list of values
                in the candidate dataset.
            config: Optional detector configuration.

        Returns:
            :class:`DriftResult` — normalized framework-level verdict.
        """
        raise NotImplementedError


# ---------------------------------------------------------------------- #
# Reference implementation — ScipyDriftDetector
# ---------------------------------------------------------------------- #


class ScipyDriftDetector(DriftDetector):
    """scipy-backed drift detector.

    Uses the Kolmogorov–Smirnov two-sample test for numerical
    features and a chi-square test for categorical features. If scipy
    is not installed, falls back to a mean/std-shift detector so the
    framework remains importable in minimal environments.
    """

    def __init__(self, use_scipy: bool = True) -> None:
        self._use_scipy = use_scipy
        self._scipy_stats = None
        if use_scipy:
            self._scipy_stats = _try_import_scipy()

    def detect(
        self,
        reference_data: dict[str, list[float]],
        current_data: dict[str, list[float]],
        config: DriftConfig | None = None,
    ) -> DriftResult:
        cfg = config or DriftConfig()
        features = sorted(set(reference_data) | set(current_data))

        # The correction has to be decided before any feature is judged,
        # so count the features that will actually receive a statistical
        # test. Ones that fall to `insufficient_samples` produce no
        # p-value and so contribute no chance of a false positive —
        # counting them would only weaken the threshold for everything
        # else.
        testable = [
            f
            for f in features
            if len(reference_data.get(f, [])) >= cfg.min_samples
            and len(current_data.get(f, [])) >= cfg.min_samples
        ]
        effective_threshold = self._corrected_threshold(cfg, len(testable))
        applied = replace(cfg, threshold=effective_threshold)

        feature_results: list[FeatureDrift] = []
        method_summary: dict[str, int] = {}
        for feature in features:
            ref = list(reference_data.get(feature, []))
            cur = list(current_data.get(feature, []))
            if self._is_categorical(ref, cur):
                res = self._chi2_drift(feature, ref, cur, applied)
            else:
                res = self._numerical_drift(feature, ref, cur, applied)
            feature_results.append(res)
            method_summary[res.method] = method_summary.get(res.method, 0) + 1

        drift_detected = any(r.drift_detected for r in feature_results)
        if feature_results:
            score = max((r.score for r in feature_results), default=0.0)
        else:
            score = 0.0

        backing = (
            "scipy-backed" if self._scipy_stats is not None
            else "fallback mean/std detector"
        )
        if cfg.correction == "bonferroni":
            notes = (
                f"{backing}; Bonferroni correction over {len(testable)} "
                f"tested feature(s): alpha {cfg.threshold} -> "
                f"{effective_threshold:.3e}"
            )
        else:
            notes = backing

        return DriftResult(
            drift_detected=drift_detected,
            # The threshold that was actually applied, not the nominal
            # one — this is the number someone needs to reproduce the
            # verdict, and `notes` records where it came from.
            threshold=effective_threshold,
            score=score,
            method="mixed",
            feature_results=feature_results,
            method_summary=method_summary,
            notes=notes,
        )

    @staticmethod
    def _corrected_threshold(cfg: DriftConfig, n_tested: int) -> float:
        """Apply the multiple-comparisons correction, if any."""
        if cfg.correction == "bonferroni":
            return cfg.threshold / max(n_tested, 1)
        if cfg.correction not in ("none", "", None):
            raise ValueError(
                f"unknown drift correction {cfg.correction!r}; "
                f"expected 'none' or 'bonferroni'"
            )
        return cfg.threshold

    # ------------------------------------------------------------------ #
    # Feature-level detection
    # ------------------------------------------------------------------ #

    def _numerical_drift(
        self,
        feature: str,
        ref: list[float],
        cur: list[float],
        cfg: DriftConfig,
    ) -> FeatureDrift:
        if len(ref) < cfg.min_samples or len(cur) < cfg.min_samples:
            return FeatureDrift(
                feature=feature,
                method="insufficient_samples",
                score=0.0,
                drift_detected=False,
                detail=(
                    f"reference={len(ref)} current={len(cur)} "
                    f"(min={cfg.min_samples})"
                ),
            )
        if self._scipy_stats is not None:
            try:
                ks = self._scipy_stats.ks_2samp(ref, cur)
                p_value = float(ks.pvalue)
                score = float(ks.statistic)
                drift = p_value < cfg.threshold
                return FeatureDrift(
                    feature=feature,
                    method="ks",
                    score=score,
                    drift_detected=drift,
                    p_value=p_value,
                    detail=(
                        f"KS statistic={score:.4f}, "
                        f"p-value={p_value:.4f}, "
                        f"threshold={cfg.threshold}"
                    ),
                )
            except Exception as exc:  # pragma: no cover - defensive
                # fall through to the simple detector
                self._scipy_stats = None
                self._last_ks_error = str(exc)
        # Fallback: mean/std shift
        return _mean_std_drift(feature, ref, cur, cfg)

    def _chi2_drift(
        self,
        feature: str,
        ref: list[float],
        cur: list[float],
        cfg: DriftConfig,
    ) -> FeatureDrift:
        if len(ref) < cfg.min_samples or len(cur) < cfg.min_samples:
            return FeatureDrift(
                feature=feature,
                method="insufficient_samples",
                score=0.0,
                drift_detected=False,
                detail=(
                    f"reference={len(ref)} current={len(cur)} "
                    f"(min={cfg.min_samples})"
                ),
            )
        if self._scipy_stats is not None:
            try:
                observed_freq = _category_counts(ref)
                expected_freq = _category_counts(cur)
                # Align categories.
                categories = sorted(
                    set(observed_freq) | set(expected_freq)
                )
                observed = [observed_freq.get(c, 0) for c in categories]
                expected = [expected_freq.get(c, 0) for c in categories]
                result = self._scipy_stats.chisquare(
                    f_obs=observed, f_exp=expected
                )
                p_value = float(result.pvalue)
                score = float(result.statistic)
                drift = p_value < cfg.threshold
                return FeatureDrift(
                    feature=feature,
                    method="chi2",
                    score=score,
                    drift_detected=drift,
                    p_value=p_value,
                    detail=(
                        f"chi-square={score:.4f}, "
                        f"p-value={p_value:.4f}, "
                        f"threshold={cfg.threshold}"
                    ),
                )
            except Exception:  # pragma: no cover - defensive
                self._scipy_stats = None
        return _mean_std_drift(feature, ref, cur, cfg)

    @staticmethod
    def _is_categorical(ref: list[float], cur: list[float]) -> bool:
        sample = (ref[:50] if len(ref) >= 50 else ref) + (
            cur[:50] if len(cur) >= 50 else cur
        )
        if not sample:
            return False
        # Heuristic: if all values are integers and there are relatively
        # few distinct ones, treat as categorical.
        all_int = all(float(v).is_integer() for v in sample)
        if not all_int:
            return False
        distinct = len(set(sample))
        return distinct <= max(20, len(sample) // 5)


# ---------------------------------------------------------------------- #
# Drift service — persists evaluations
# ---------------------------------------------------------------------- #


class DriftService:
    """High-level drift service.

    Wraps a :class:`DriftDetector` and persists each evaluation. The
    service itself does not pull data — callers pass
    :class:`DatasetVersion` objects along with feature values.
    """

    def __init__(
        self,
        session: Session,
        detector: DriftDetector,
    ) -> None:
        self._session = session
        self._detector = detector

    def evaluate(
        self,
        reference_version: DatasetVersion,
        current_version: DatasetVersion,
        reference_data: dict[str, list[float]],
        current_data: dict[str, list[float]],
        config: DriftConfig | None = None,
        notes: str = "",
    ) -> DriftResult:
        """Run the detector and persist the result.

        ``reference_data`` and ``current_data`` are feature-value
        mappings. The framework does not load data itself; the
        caller does.
        """
        if config is None:
            # Deferred import: framework_settings.manager imports
            # DriftConfig from this module, so importing it back at
            # module level here would be circular. Resolved here
            # rather than inside ScipyDriftDetector.detect() (whose own
            # ``config or DriftConfig()`` fallback stays as a bare
            # default for direct/pluggable-detector callers that
            # bypass this service) — DriftDetector is meant to be
            # swappable (see its own docstring), so "read the
            # framework's persisted default" belongs in the framework's
            # own orchestration layer around it, not baked into one
            # specific implementation.
            from mlops_framework.framework_settings.manager import (
                FrameworkSettingsManager,
            )

            config = FrameworkSettingsManager(self._session).get_drift_config()
        result = self._detector.detect(reference_data, current_data, config)
        outcome = (
            DriftOutcome.DRIFT_DETECTED
            if result.drift_detected
            else DriftOutcome.NO_DRIFT
        )
        row = DriftEvaluation(
            reference_dataset_version_id=reference_version.id,
            current_dataset_version_id=current_version.id,
            method=result.method,
            outcome=outcome,
            score=result.score,
            threshold=result.threshold,
            details_json=json.dumps(result.to_dict()),
            notes=notes or None,
        )
        self._session.add(row)
        self._session.flush()
        return result

    def get_evaluations(
        self, dataset_version_id: int
    ) -> list[DriftEvaluation]:
        """Return all drift evaluations involving a dataset version."""
        return list(
            self._session.execute(
                select(DriftEvaluation).where(
                    (DriftEvaluation.reference_dataset_version_id
                     == dataset_version_id)
                    | (DriftEvaluation.current_dataset_version_id
                       == dataset_version_id)
                ).order_by(DriftEvaluation.created_at.desc())
            ).scalars().all()
        )


# ---------------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------------- #


def _try_import_scipy():
    try:
        from scipy import stats  # type: ignore
    except ImportError:
        return None
    return stats


def _mean_std_drift(
    feature: str,
    ref: list[float],
    cur: list[float],
    cfg: DriftConfig,
) -> FeatureDrift:
    if not ref or not cur:
        return FeatureDrift(
            feature=feature,
            method="meanstd",
            score=0.0,
            drift_detected=False,
            detail="empty population",
        )
    mean_ref = sum(ref) / len(ref)
    mean_cur = sum(cur) / len(cur)
    std_ref = _std(ref, mean_ref)
    std_cur = _std(cur, mean_cur)
    diff = abs(mean_cur - mean_ref)
    pooled = max(std_ref, std_cur, 1e-9)
    score = diff / pooled
    drift = score > cfg.threshold
    return FeatureDrift(
        feature=feature,
        method="meanstd",
        score=score,
        drift_detected=drift,
        detail=(
            f"mean shift {diff:.4f} / pooled std {pooled:.4f}"
        ),
    )


def _std(values: list[float], mean: float) -> float:
    if len(values) < 2:
        return 0.0
    var = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return math.sqrt(var)


def _category_counts(values: list[float]) -> dict[float, int]:
    counts: dict[float, int] = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    return counts
