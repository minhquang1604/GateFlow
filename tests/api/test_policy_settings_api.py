"""Live tests for the persisted policy settings API
(``/api/settings/policies*``) and its wiring into the two existing call
sites it changes: ``api/routers/internal.py``'s readiness/promote
endpoints, and the scheduler's ``run_schedule_now``
(``scheduling/runner.py``).

Two properties matter here, per the plan this checkpoint implements:

1. **Backward compatibility** — with an empty ``framework_settings``
   table, every rewired call site behaves *exactly* like it did before
   this checkpoint (bare dataclass defaults, same as the old hardcoded
   literals produce for anyone who's never touched Settings).
2. **It actually works** — once a setting is customized, a real
   decision (readiness, promotion) reflects it, and resetting reverts
   the decision too.

``client``/``session_factory`` come from ``tests/api/conftest.py``
(fresh in-memory SQLite per test, real transaction handling — no
overridden ``get_db``). The one scheduler-level test needs a real
MLflow tracking URI (``run_schedule_now`` constructs its own
``MLflowTracker``), so it gets its own fixture mirroring
``test_schedules_api.py``'s, and is skipped if the mlflow SDK isn't
installed.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# tests/api has no __init__.py, so pytest puts this directory on sys.path
# and the shared fixtures module is importable by its bare name.
from conftest import authenticated_client  # noqa: E402
from mlops_framework.database.models.dataset import Dataset
from mlops_framework.database.models.dataset_version import DatasetVersion
from mlops_framework.database.models.model import Model as ModelRow
from mlops_framework.database.models.training_run import RunStatus, TrainingRun

DRIFT_DEFAULT = {
    "threshold": 0.05,
    "min_samples": 30,
    "methods": ["ks", "chi2"],
    # "none" keeps the historical any-feature-significant behaviour for
    # callers that have not opted into a correction — see
    # DriftConfig.correction.
    "correction": "none",
}


# ---------------------------------------------------------------------- #
# /api/settings/policies* — CRUD
# ---------------------------------------------------------------------- #


class TestPolicyCrud:
    def test_list_defaults(self, client):
        resp = client.get("/api/settings/policies")
        assert resp.status_code == 200
        body = resp.json()
        assert set(body) == {"promotion", "eligibility", "training_policy", "drift"}
        assert all(entry["is_default"] for entry in body.values())
        assert body["drift"]["value"] == DRIFT_DEFAULT

    def test_get_single_default(self, client):
        resp = client.get("/api/settings/policies/drift")
        assert resp.status_code == 200
        assert resp.json() == {"value": DRIFT_DEFAULT, "is_default": True}

    def test_get_unknown_key_is_404(self, client):
        assert client.get("/api/settings/policies/not_a_key").status_code == 404

    def test_put_round_trip_and_marks_customized(self, client):
        resp = client.put("/api/settings/policies/drift", json={"value": {"threshold": 0.2}})
        assert resp.status_code == 200
        body = resp.json()
        # min_samples/methods/correction weren't in the request —
        # normalized back to their own defaults, same as constructing
        # DriftConfig(threshold=0.2) directly would.
        assert body["value"] == {**DRIFT_DEFAULT, "threshold": 0.2}
        assert body["is_default"] is False

        listed = client.get("/api/settings/policies").json()
        assert listed["drift"]["is_default"] is False
        assert listed["drift"]["value"]["threshold"] == 0.2
        # Untouched policies stay default.
        assert listed["promotion"]["is_default"] is True

    def test_put_unknown_key_is_404(self, client):
        resp = client.put("/api/settings/policies/not_a_key", json={"value": {}})
        assert resp.status_code == 404

    def test_put_malformed_value_is_422(self, client):
        resp = client.put(
            "/api/settings/policies/training_policy",
            json={"value": {"required_size": "not-a-number"}},
        )
        assert resp.status_code == 422
        # And nothing was persisted.
        assert client.get("/api/settings/policies/training_policy").json()["is_default"] is True

    def test_reset_reverts(self, client):
        client.put("/api/settings/policies/drift", json={"value": {"threshold": 0.9}})
        resp = client.post("/api/settings/policies/drift/reset")
        assert resp.status_code == 200
        assert resp.json() == {"value": DRIFT_DEFAULT, "is_default": True}
        assert client.get("/api/settings/policies/drift").json()["is_default"] is True

    def test_reset_unknown_key_is_404(self, client):
        assert client.post("/api/settings/policies/not_a_key/reset").status_code == 404

    def test_writes_are_audited(self, client):
        client.put(
            "/api/settings/policies/eligibility",
            json={"value": {"cooldown_hours": 6}},
            headers={"X-Actor": "alice"},
        )
        entries = client.get("/api/audit?limit=10").json()
        matches = [e for e in entries if e["action"] == "SETTINGS_UPDATED"]
        assert len(matches) == 1
        assert matches[0]["actor"] == "alice"
        assert matches[0]["entity_type"] == "FrameworkSetting"

        client.post("/api/settings/policies/eligibility/reset")
        entries = client.get("/api/audit?limit=10").json()
        assert any(e["action"] == "SETTINGS_RESET" for e in entries)


# ---------------------------------------------------------------------- #
# Readiness endpoint wiring (api/routers/internal.py)
# ---------------------------------------------------------------------- #


def _seed_dataset_version(session_factory, *, row_count: int = 1000) -> int:
    s = session_factory()
    try:
        ds = Dataset(name="ds")
        s.add(ds)
        s.flush()
        dv = DatasetVersion(
            dataset_id=ds.id,
            version_number=1,
            storage_uri="s3://bucket/v1.parquet",
            checksum="a" * 64,
            schema_hash="b" * 64,
            row_count=row_count,
        )
        s.add(dv)
        s.commit()
        return dv.id
    finally:
        s.close()


class TestReadinessWiring:
    def test_backward_compatible_when_settings_empty(self, client, session_factory):
        """Empty framework_settings ⇒ identical to today's bare
        TrainingPolicy() — required_size=0, so any dataset is READY."""
        version_id = _seed_dataset_version(session_factory, row_count=1000)
        resp = client.post(f"/api/internal/readiness/{version_id}", json={"policy": {}})
        assert resp.status_code == 200
        assert resp.json()["is_ready"] is True

    def test_persisted_required_size_actually_blocks_and_reset_reverts(self, client, session_factory):
        version_id = _seed_dataset_version(session_factory, row_count=1000)

        client.put(
            "/api/settings/policies/training_policy",
            json={"value": {"required_size": 999_999}},
        )
        blocked = client.post(f"/api/internal/readiness/{version_id}", json={"policy": {}})
        assert blocked.status_code == 200
        assert blocked.json()["is_ready"] is False

        client.post("/api/settings/policies/training_policy/reset")
        ready_again = client.post(f"/api/internal/readiness/{version_id}", json={"policy": {}})
        assert ready_again.json()["is_ready"] is True

    def test_request_policy_overrides_persisted_base(self, client, session_factory):
        """A per-call request.policy still wins over the persisted
        default — the merge is base-then-override, not the other way."""
        version_id = _seed_dataset_version(session_factory, row_count=1000)
        client.put(
            "/api/settings/policies/training_policy",
            json={"value": {"required_size": 999_999}},
        )
        resp = client.post(
            f"/api/internal/readiness/{version_id}", json={"policy": {"required_size": 0}}
        )
        assert resp.json()["is_ready"] is True


# ---------------------------------------------------------------------- #
# Promote endpoint wiring (api/routers/internal.py)
# ---------------------------------------------------------------------- #


def _seed_promote(session_factory) -> dict[str, int]:
    s = session_factory()
    try:
        ds = Dataset(name="churn")
        s.add(ds)
        s.flush()
        dv = DatasetVersion(
            dataset_id=ds.id, version_number=1, storage_uri="s3://bucket/v1.parquet",
            checksum="a" * 64, schema_hash="b" * 64, row_count=1000,
        )
        s.add(dv)
        s.flush()
        run = TrainingRun(
            dataset_version_id=dv.id,
            status=RunStatus.SUCCESS.value,
            pipeline_id="case_studies.churn.pipelines:train",
            metadata_json=json.dumps({"model_name": "churn-xgboost"}),
        )
        s.add(run)
        s.flush()
        model = ModelRow(name="churn-xgboost", task="classification")
        s.add(model)
        s.flush()
        ids = {"dataset_version_id": dv.id, "training_run_id": run.id, "model_id": model.id}
        s.commit()
        return ids
    finally:
        s.close()


def _promote_body(ids: dict[str, int], **overrides) -> dict:
    body = {
        "dataset_version_id": ids["dataset_version_id"],
        "training_run_id": ids["training_run_id"],
        "mlflow_run_id": "mlflow-run-abc",
        "metrics": {"f1": 0.91},
        "artifact_uri": "s3://bucket/model.pkl",
        "min_f1": 0.5,
    }
    body.update(overrides)
    return body


class TestPromoteWiring:
    def test_backward_compatible_when_settings_empty(self, client, session_factory):
        ids = _seed_promote(session_factory)
        resp = client.post(
            "/api/internal/models/churn-xgboost/promote", json=_promote_body(ids)
        )
        assert resp.status_code == 200
        assert resp.json()["promoted"] is True

    def test_persisted_min_floors_blocks_promotion(self, client, session_factory):
        """min_floors is the one PromotionConfig field this endpoint
        doesn't already override per-call (min_metrics/must_beat_production/
        allow_cold_start all get overwritten from the request) — a
        persisted floor is the field that actually reaches the decision."""
        ids = _seed_promote(session_factory)
        client.put(
            "/api/settings/policies/promotion",
            json={"value": {"min_floors": {"f1": 0.99}}},
        )
        resp = client.post(
            "/api/internal/models/churn-xgboost/promote",
            json=_promote_body(ids, metrics={"f1": 0.91}),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["promoted"] is False
        assert any("below floor" in r for r in body["reasons"])


# ---------------------------------------------------------------------- #
# Scheduler wiring (scheduling/runner.py's _fire, via /schedules/{id}/run-now)
# ---------------------------------------------------------------------- #

pytest.importorskip("mlflow", reason="mlflow SDK is not installed")

from mlops_framework.api.app import create_app  # noqa: E402
from mlops_framework.api.deps import get_db_manager_dep  # noqa: E402
from mlops_framework.config.settings import get_settings  # noqa: E402
from mlops_framework.database.base import Base  # noqa: E402
from mlops_framework.database.session import DatabaseManager  # noqa: E402

PIPELINE_ID = "tests._pipelines.e2e_training:main"


@pytest.fixture()
def mlflow_uri(tmp_path, monkeypatch):
    uri = f"sqlite:///{tmp_path}/mlflow.db"
    monkeypatch.setenv("MLFLOW_TRACKING_URI", uri)
    get_settings.cache_clear()
    yield uri
    get_settings.cache_clear()


@pytest.fixture()
def scheduler_api(mlflow_uri):
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    mgr = DatabaseManager()
    mgr._engine = engine
    mgr._session_factory = session_factory

    app = create_app(mount_ui=False)
    app.dependency_overrides[get_db_manager_dep] = lambda: mgr
    yield authenticated_client(app), session_factory
    Base.metadata.drop_all(engine)
    engine.dispose()


def _seed_schedule(session_factory) -> dict[str, int]:
    s = session_factory()
    try:
        ds = Dataset(name="churn")
        s.add(ds)
        s.flush()
        s.add(DatasetVersion(
            dataset_id=ds.id, version_number=1, storage_uri="s3://b/v1.csv",
            checksum="a" * 64, schema_hash="b" * 64, row_count=1000,
        ))
        model = ModelRow(name="churn-xgboost-sched", task="classification")
        s.add(model)
        s.flush()
        s.commit()
        return {"dataset_id": ds.id, "model_id": model.id}
    finally:
        s.close()


class TestSchedulerWiring:
    def test_backward_compatible_when_settings_empty(self, scheduler_api):
        """Empty framework_settings ⇒ the schedule fires and promotes
        exactly like test_schedules_api.py's existing run-now test
        (which predates this checkpoint) already asserts."""
        client, sf = scheduler_api
        ids = _seed_schedule(sf)
        created = client.post("/api/schedules", json={
            "model_id": ids["model_id"], "dataset_id": ids["dataset_id"],
            "pipeline_id": PIPELINE_ID, "cron_expression": "0 3 1 1 *",
            "min_f1": 0.5,
        }).json()
        resp = client.post(f"/api/schedules/{created['id']}/run-now")
        assert resp.status_code == 200
        body = resp.json()
        assert body["fired"] is True
        assert body["promoted"] is True

    def test_persisted_min_floors_blocks_a_schedules_own_promotion(self, scheduler_api):
        """_fire() passes eligibility_config straight from Settings, but
        force=True (the module's own "the cron IS the eligibility
        decision") short-circuits eligibility regardless of its content
        — so eligibility settings have no observable effect via this
        call site specifically (pre-existing, not introduced by this
        checkpoint). Promotion isn't short-circuited by force, and this
        call site only overrides min_metrics/must_beat_production/
        allow_cold_start per-call — min_floors is the field a persisted
        setting actually reaches here. The synthetic e2e_training
        pipeline logs f1 in 0.80-0.849, so a 0.99 floor always blocks."""
        client, sf = scheduler_api
        ids = _seed_schedule(sf)
        client.put(
            "/api/settings/policies/promotion",
            json={"value": {"min_floors": {"f1": 0.99}}},
        )
        created = client.post("/api/schedules", json={
            "model_id": ids["model_id"], "dataset_id": ids["dataset_id"],
            "pipeline_id": PIPELINE_ID, "cron_expression": "0 3 1 1 *",
            "min_f1": 0.5,
        }).json()

        resp = client.post(f"/api/schedules/{created['id']}/run-now")
        assert resp.status_code == 200
        body = resp.json()
        assert body["fired"] is True
        assert body["training_run_id"] is not None  # training itself still ran
        assert body["promoted"] is False
        # RunNowResponse only carries the fixed blocked_reason code, not
        # the actual PromotionDecision.reasons text — that detail lands
        # in the MODEL_REJECTED audit entry's metadata instead.
        assert body["blocked_reason"] == "model_rejected"
        audit_entries = client.get("/api/audit?limit=10").json()
        rejected = [e for e in audit_entries if e["action"] == "MODEL_REJECTED"]
        assert len(rejected) == 1
        assert any("below floor" in r for r in rejected[0]["metadata"]["reasons"])
