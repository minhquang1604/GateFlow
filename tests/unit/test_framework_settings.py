"""Unit tests for FrameworkSettingsManager."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sqlalchemy import select

from mlops_framework.audit.manager import AuditManager
from mlops_framework.database.base import Base
from mlops_framework.database.models.framework_setting import FrameworkSetting
from mlops_framework.drift.detector import DriftConfig
from mlops_framework.framework_settings.manager import (
    DRIFT,
    ELIGIBILITY,
    PROMOTION,
    TRAINING_POLICY,
    FrameworkSettingsManager,
)
from mlops_framework.governance.eligibility import EligibilityConfig
from mlops_framework.governance.promotion import PromotionConfig
from mlops_framework.readiness.engine import TrainingPolicy


@pytest.fixture()
def session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


class TestDefaults:
    """With nothing persisted, every typed getter returns the bare
    dataclass default — same objects a caller gets from `Cls()`."""

    def test_promotion_default(self, session):
        mgr = FrameworkSettingsManager(session)
        assert mgr.get_promotion_config() == PromotionConfig()

    def test_eligibility_default(self, session):
        mgr = FrameworkSettingsManager(session)
        assert mgr.get_eligibility_config() == EligibilityConfig()

    def test_training_policy_default(self, session):
        mgr = FrameworkSettingsManager(session)
        assert mgr.get_training_policy() == TrainingPolicy()

    def test_drift_default(self, session):
        mgr = FrameworkSettingsManager(session)
        assert mgr.get_drift_config() == DriftConfig()

    def test_get_raw_none_when_unset(self, session):
        mgr = FrameworkSettingsManager(session)
        assert mgr.get_raw(PROMOTION) is None

    def test_list_effective_all_default(self, session):
        mgr = FrameworkSettingsManager(session)
        effective = mgr.list_effective()
        assert set(effective) == {PROMOTION, ELIGIBILITY, TRAINING_POLICY, DRIFT}
        assert all(entry["is_default"] for entry in effective.values())
        assert effective[PROMOTION]["value"] == PromotionConfig().to_dict()
        assert effective[DRIFT]["value"] == DriftConfig().to_dict()


class TestSetAndGet:
    def test_promotion_round_trip(self, session):
        mgr = FrameworkSettingsManager(session)
        mgr.set_raw(PROMOTION, {"min_metrics": {"f1": 0.9}, "must_beat_production": False})
        cfg = mgr.get_promotion_config()
        assert cfg.min_metrics == {"f1": 0.9}
        assert cfg.must_beat_production is False
        # Untouched fields keep their dataclass defaults.
        assert cfg.allow_cold_start is True

    def test_eligibility_round_trip_including_previously_unknown_keys(self, session):
        mgr = FrameworkSettingsManager(session)
        # Exercises the from_dict fix: an unrecognised key must not raise.
        mgr.set_raw(
            ELIGIBILITY,
            {"cooldown_hours": 12, "min_new_rows": 100, "not_a_real_field": "ignored"},
        )
        cfg = mgr.get_eligibility_config()
        assert cfg.cooldown_hours == 12
        assert cfg.min_new_rows == 100

    def test_training_policy_round_trip(self, session):
        mgr = FrameworkSettingsManager(session)
        mgr.set_raw(TRAINING_POLICY, {"required_size": 500, "required_columns": ["a", "b"]})
        policy = mgr.get_training_policy()
        assert policy.required_size == 500
        assert policy.required_columns == ["a", "b"]

    def test_drift_round_trip(self, session):
        mgr = FrameworkSettingsManager(session)
        mgr.set_raw(DRIFT, {"threshold": 0.1, "min_samples": 50})
        cfg = mgr.get_drift_config()
        assert cfg.threshold == 0.1
        assert cfg.min_samples == 50
        assert cfg.methods == ["ks", "chi2"]

    def test_set_raw_returns_normalized_value(self, session):
        mgr = FrameworkSettingsManager(session)
        normalized = mgr.set_raw(DRIFT, {"threshold": 0.2})
        assert normalized == {"threshold": 0.2, "min_samples": 30, "methods": ["ks", "chi2"]}

    def test_second_set_updates_in_place_not_duplicates(self, session):
        mgr = FrameworkSettingsManager(session)
        mgr.set_raw(DRIFT, {"threshold": 0.1})
        mgr.set_raw(DRIFT, {"threshold": 0.2})
        assert mgr.get_drift_config().threshold == 0.2
        rows = session.execute(select(FrameworkSetting)).scalars().all()
        assert len(rows) == 1

    def test_list_effective_marks_customized(self, session):
        mgr = FrameworkSettingsManager(session)
        mgr.set_raw(PROMOTION, {"must_beat_production": False})
        effective = mgr.list_effective()
        assert effective[PROMOTION]["is_default"] is False
        assert effective[ELIGIBILITY]["is_default"] is True


class TestValidation:
    def test_unknown_key_raises_key_error_on_get(self, session):
        mgr = FrameworkSettingsManager(session)
        with pytest.raises(KeyError):
            mgr.get_raw("not_a_real_key")

    def test_unknown_key_raises_key_error_on_set(self, session):
        mgr = FrameworkSettingsManager(session)
        with pytest.raises(KeyError):
            mgr.set_raw("not_a_real_key", {})

    def test_malformed_value_raises(self, session):
        mgr = FrameworkSettingsManager(session)
        # required_size must be int-coercible; a non-numeric string isn't.
        with pytest.raises((TypeError, ValueError)):
            mgr.set_raw(TRAINING_POLICY, {"required_size": "not-a-number"})


class TestReset:
    def test_reset_reverts_to_default(self, session):
        mgr = FrameworkSettingsManager(session)
        mgr.set_raw(DRIFT, {"threshold": 0.9})
        assert mgr.get_drift_config().threshold == 0.9
        mgr.reset(DRIFT)
        assert mgr.get_drift_config() == DriftConfig()
        assert mgr.get_raw(DRIFT) is None

    def test_reset_when_never_set_is_a_no_op(self, session):
        mgr = FrameworkSettingsManager(session)
        mgr.reset(PROMOTION)  # must not raise
        assert mgr.get_promotion_config() == PromotionConfig()

    def test_reset_unknown_key_raises(self, session):
        mgr = FrameworkSettingsManager(session)
        with pytest.raises(KeyError):
            mgr.reset("not_a_real_key")


class TestComposesWithAuditManager:
    """A settings-write endpoint composes this manager with
    AuditManager exactly like ScheduleManager's router does — this is
    the shape CP2's router will follow."""

    def test_set_raw_then_audit_record_lands(self, session):
        mgr = FrameworkSettingsManager(session)
        am = AuditManager(session)

        normalized = mgr.set_raw(ELIGIBILITY, {"cooldown_hours": 6})
        entry = am.record(
            actor="alice",
            action="SETTINGS_UPDATED",
            entity_type="FrameworkSetting",
            metadata={"key": ELIGIBILITY, "value": normalized},
        )

        assert entry is not None
        entries = am.list_entries(entity_type="FrameworkSetting")
        assert len(entries) == 1
        assert entries[0].actor == "alice"
        assert entries[0].action == "SETTINGS_UPDATED"
        assert '"cooldown_hours": 6' in entries[0].metadata_json
