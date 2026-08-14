"""``/api/api-keys`` — the ``admin`` scope's whole surface.

The manager is covered in ``tests/unit/test_api_keys.py``. What matters
here is the gate (only ``admin`` reaches these routes, and the shared
secret does not), and that the plaintext appears in exactly one response
body ever.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from conftest import WRITE_TOKEN  # noqa: E402 - see the note in test_schedules_api.py
from mlops_framework.auth.manager import ApiKeyManager


def _admin_client(app, session_factory, name="root"):
    s = session_factory()
    try:
        key = ApiKeyManager(s).create(name=name, scopes=["admin"]).plaintext
        s.commit()
    finally:
        s.close()
    return TestClient(app, headers={"Authorization": f"Bearer {key}"})


class TestGate:
    def test_the_shared_secret_is_not_admin(self, client):
        """CONSOLE_WRITE_TOKEN grants `write`, deliberately not `admin`.
        A shared secret that could mint per-principal keys would hand
        anyone holding it the ability to manufacture identities."""
        r = client.get("/api/api-keys")
        assert r.status_code == 403
        assert "admin" in r.json()["detail"]

    def test_anonymous_is_401(self, anon_client):
        assert anon_client.get("/api/api-keys").status_code == 401

    def test_a_write_key_cannot_manage_keys(self, app, session_factory):
        s = session_factory()
        try:
            key = ApiKeyManager(s).create(name="alice", scopes=["write"]).plaintext
            s.commit()
        finally:
            s.close()
        c = TestClient(app, headers={"Authorization": f"Bearer {key}"})
        assert c.get("/api/api-keys").status_code == 403

    def test_admin_can(self, app, session_factory):
        assert _admin_client(app, session_factory).get("/api/api-keys").status_code == 200


class TestMinting:
    def test_returns_the_key_once(self, app, session_factory):
        c = _admin_client(app, session_factory)
        r = c.post("/api/api-keys", json={"name": "alice", "scopes": ["write"]})
        assert r.status_code == 201
        body = r.json()
        assert body["key"].startswith("mlops_ak_")
        assert body["name"] == "alice"
        assert body["scopes"] == ["write"]

    def test_and_never_again(self, app, session_factory):
        c = _admin_client(app, session_factory)
        c.post("/api/api-keys", json={"name": "alice", "scopes": ["write"]})

        listed = c.get("/api/api-keys").json()
        alice = next(k for k in listed if k["name"] == "alice")
        assert "key" not in alice
        # Only the identifying prefix, which is not a credential.
        assert alice["key_prefix"].startswith("mlops_ak_")

    def test_a_minted_key_works(self, app, session_factory):
        c = _admin_client(app, session_factory)
        key = c.post(
            "/api/api-keys", json={"name": "alice", "scopes": ["write"]}
        ).json()["key"]

        alice = TestClient(app, headers={"Authorization": f"Bearer {key}"})
        assert alice.post(
            "/api/internal/datasets", json={"name": "d"}
        ).status_code == 200

    def test_duplicate_name_is_409(self, app, session_factory):
        c = _admin_client(app, session_factory)
        c.post("/api/api-keys", json={"name": "alice", "scopes": ["read"]})
        r = c.post("/api/api-keys", json={"name": "alice", "scopes": ["read"]})
        assert r.status_code == 409

    def test_unknown_scope_is_422(self, app, session_factory):
        c = _admin_client(app, session_factory)
        r = c.post("/api/api-keys", json={"name": "x", "scopes": ["superuser"]})
        assert r.status_code == 422

    def test_minting_is_audited(self, app, session_factory):
        from mlops_framework.database.models.audit_log import AuditLog

        c = _admin_client(app, session_factory, name="root")
        c.post("/api/api-keys", json={"name": "alice", "scopes": ["write"]})

        s = session_factory()
        try:
            row = s.query(AuditLog).filter_by(action="API_KEY_CREATED").one()
            # Recorded against the admin who minted it, verified from
            # their own key rather than a header they filled in.
            assert row.actor == "root"
        finally:
            s.close()


class TestRevoking:
    def test_revoked_key_stops_working(self, app, session_factory):
        c = _admin_client(app, session_factory)
        key = c.post(
            "/api/api-keys", json={"name": "leaked", "scopes": ["write"]}
        ).json()["key"]
        leaked = TestClient(app, headers={"Authorization": f"Bearer {key}"})
        assert leaked.post("/api/internal/datasets", json={"name": "a"}).status_code == 200

        assert c.delete("/api/api-keys/leaked").status_code == 200
        assert leaked.post("/api/internal/datasets", json={"name": "b"}).status_code == 401

    def test_the_row_survives_revocation(self, app, session_factory):
        """Audit rows name it; it has to stay resolvable."""
        c = _admin_client(app, session_factory)
        c.post("/api/api-keys", json={"name": "leaked", "scopes": ["read"]})
        c.delete("/api/api-keys/leaked")

        assert not [k for k in c.get("/api/api-keys").json() if k["name"] == "leaked"]
        all_keys = c.get("/api/api-keys?include_revoked=true").json()
        leaked = next(k for k in all_keys if k["name"] == "leaked")
        assert leaked["revoked_at"] is not None

    def test_unknown_name_is_404(self, app, session_factory):
        assert _admin_client(app, session_factory).delete(
            "/api/api-keys/nobody"
        ).status_code == 404


class TestScopesEndpoint:
    def test_lists_the_grantable_scopes(self, app, session_factory):
        r = _admin_client(app, session_factory).get("/api/api-keys/scopes")
        assert r.status_code == 200
        assert set(r.json()) == {"read", "write", "admin"}


class TestBackwardCompatibility:
    def test_the_shared_secret_still_writes(self, client):
        """The transitional path. Removing it in the same change that
        introduced keys would break every deployment configured with
        CONSOLE_WRITE_TOKEN — the DAG included."""
        assert client.post(
            "/api/internal/datasets", json={"name": "d"}
        ).status_code == 200

    def test_a_key_and_the_secret_can_coexist(self, app, session_factory):
        s = session_factory()
        try:
            key = ApiKeyManager(s).create(name="alice", scopes=["write"]).plaintext
            s.commit()
        finally:
            s.close()

        by_key = TestClient(app, headers={"Authorization": f"Bearer {key}"})
        by_secret = TestClient(app, headers={"X-Console-Token": WRITE_TOKEN})
        assert by_key.post("/api/internal/datasets", json={"name": "a"}).status_code == 200
        assert by_secret.post("/api/internal/datasets", json={"name": "b"}).status_code == 200
