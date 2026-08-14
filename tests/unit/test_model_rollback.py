"""``ModelManager.rollback_to`` — the recovery path the registry lacked.

Before this, ARCHIVED was terminal: a model that reached PRODUCTION and
turned out to be bad could only be replaced by training a new one and
hoping it promoted. These tests pin both the happy path and, more
importantly, the refusals — a rollback that silently did the wrong thing
would be worse than not having one.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from mlops_framework.database.base import Base
from mlops_framework.database.models import (  # noqa: F401 - registers tables
    Dataset,
    DatasetVersion,
)
from mlops_framework.database.models.model_version import ModelState
from mlops_framework.exceptions import ModelVersionNotFoundError, RollbackError
from mlops_framework.model.lifecycle import is_valid_transition
from mlops_framework.model.manager import ModelManager


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


@pytest.fixture()
def model_with_two_versions(session):
    """v1 ARCHIVED (the known-good one), v2 PRODUCTION (the bad one)."""
    ds = Dataset(name="churn")
    session.add(ds)
    session.flush()
    dv = DatasetVersion(
        dataset_id=ds.id, version_number=1, storage_uri="s3://b/v1.csv",
        row_count=100, checksum="c", schema_hash="h",
    )
    session.add(dv)
    session.flush()

    mm = ModelManager(session)
    model = mm.create_model("churn-clf")
    # state=CANDIDATE on creation, the same way the promotion paths do
    # it (see api/routers/internal.py) — the column default is TRAINING.
    v1 = mm.create_model_version(
        model_id=model.id, dataset_version_id=dv.id,
        state=ModelState.CANDIDATE, metrics={"f1": 0.90},
    )
    v2 = mm.create_model_version(
        model_id=model.id, dataset_version_id=dv.id,
        state=ModelState.CANDIDATE, metrics={"f1": 0.95},
    )
    for v in (v1, v2):
        mm.transition_state(v.id, ModelState.APPROVED)
    mm.transition_state(v1.id, ModelState.PRODUCTION)
    mm.transition_state(v1.id, ModelState.ARCHIVED)
    mm.transition_state(v2.id, ModelState.PRODUCTION)
    session.commit()
    return mm, model, v1, v2


class TestStateMachine:
    def test_archived_can_be_re_approved(self):
        assert is_valid_transition(ModelState.ARCHIVED, ModelState.APPROVED)

    def test_archived_still_cannot_go_straight_to_production(self):
        """The edge lands on APPROVED on purpose — rollback_to walks the
        two steps, so nothing else acquires a shortcut into PRODUCTION."""
        assert not is_valid_transition(ModelState.ARCHIVED, ModelState.PRODUCTION)

    def test_rejected_is_still_terminal(self):
        for target in ModelState:
            assert not is_valid_transition(ModelState.REJECTED, target)


class TestRollback:
    def test_restores_the_archived_version(self, session, model_with_two_versions):
        mm, model, v1, v2 = model_with_two_versions

        result = mm.rollback_to(v1.id)
        session.commit()

        assert mm.get_model_version(v1.id).state == ModelState.PRODUCTION
        assert result.restored_version_id == v1.id
        assert result.restored_version_number == v1.version_number
        assert result.model_name == "churn-clf"

    def test_archives_the_incumbent(self, session, model_with_two_versions):
        mm, model, v1, v2 = model_with_two_versions

        result = mm.rollback_to(v1.id)
        session.commit()

        assert mm.get_model_version(v2.id).state == ModelState.ARCHIVED
        assert result.previous_production_id == v2.id
        assert result.previous_production_number == v2.version_number

    def test_never_two_production_versions(self, session, model_with_two_versions):
        """The partial unique index would reject it, so reaching this
        assertion at all is the point — the swap orders its two writes."""
        mm, model, v1, v2 = model_with_two_versions

        mm.rollback_to(v1.id)
        session.commit()

        live = [
            v for v in mm.list_model_versions(model.id)
            if v.state == ModelState.PRODUCTION
        ]
        assert len(live) == 1
        assert live[0].id == v1.id

    def test_ignores_metrics(self, session, model_with_two_versions):
        """The incumbent here has the *better* f1 (0.95 vs 0.90). A
        rollback must still go through: the promotion policy answers a
        different question, and gating on metrics would block the
        rollback in exactly the case it exists for."""
        mm, model, v1, v2 = model_with_two_versions
        assert mm.get_metrics(v2.id)["f1"] > mm.get_metrics(v1.id)["f1"]

        mm.rollback_to(v1.id)
        session.commit()

        assert mm.get_model_version(v1.id).state == ModelState.PRODUCTION

    def test_works_when_nothing_is_in_production(self, session, model_with_two_versions):
        mm, model, v1, v2 = model_with_two_versions
        mm.transition_state(v2.id, ModelState.ARCHIVED)
        session.commit()

        result = mm.rollback_to(v1.id)
        session.commit()

        assert result.previous_production_id is None
        assert result.previous_production_number is None
        assert mm.get_model_version(v1.id).state == ModelState.PRODUCTION

    def test_can_roll_back_again(self, session, model_with_two_versions):
        """v1 back in, then v2 back in — the edge is not single-use."""
        mm, model, v1, v2 = model_with_two_versions

        mm.rollback_to(v1.id)
        session.commit()
        mm.rollback_to(v2.id)
        session.commit()

        assert mm.get_model_version(v2.id).state == ModelState.PRODUCTION
        assert mm.get_model_version(v1.id).state == ModelState.ARCHIVED


class TestRefusals:
    def test_unknown_version(self, session, model_with_two_versions):
        mm, *_ = model_with_two_versions
        with pytest.raises(ModelVersionNotFoundError):
            mm.rollback_to(9999)

    def test_already_production(self, session, model_with_two_versions):
        mm, model, v1, v2 = model_with_two_versions
        with pytest.raises(RollbackError, match="already the PRODUCTION"):
            mm.rollback_to(v2.id)

    @pytest.mark.parametrize(
        "state", [ModelState.CANDIDATE, ModelState.REJECTED, ModelState.TRAINING]
    )
    def test_a_version_that_was_never_good(self, session, model_with_two_versions, state):
        """CANDIDATE/TRAINING have never been in production and REJECTED
        failed the policy — none is a known-good version to return to."""
        mm, model, v1, v2 = model_with_two_versions
        dv_id = v1.dataset_version_id
        fresh = mm.create_model_version(
            model_id=model.id,
            dataset_version_id=dv_id,
            state=ModelState.TRAINING if state == ModelState.TRAINING else ModelState.CANDIDATE,
        )
        if state == ModelState.REJECTED:
            mm.transition_state(fresh.id, ModelState.REJECTED)
        session.commit()

        with pytest.raises(RollbackError, match="only an"):
            mm.rollback_to(fresh.id)

    def test_a_refused_rollback_changes_nothing(self, session, model_with_two_versions):
        mm, model, v1, v2 = model_with_two_versions
        with pytest.raises(RollbackError):
            mm.rollback_to(v2.id)
        session.rollback()

        assert mm.get_model_version(v2.id).state == ModelState.PRODUCTION
        assert mm.get_model_version(v1.id).state == ModelState.ARCHIVED
