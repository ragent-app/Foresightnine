from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app, reset_state


def test_local_mock_registration_login_logout_and_hard_delete(monkeypatch) -> None:
    monkeypatch.setenv("FORESIGHTNINE_LOCAL_AUTH_MODE", "mock")
    reset_state()
    client = TestClient(app)
    credentials = {"email": "Stage.User@example.test", "password": "correct horse battery staple!"}

    registered = client.post("/v1/auth/local/register", json=credentials)
    assert registered.status_code == 201, registered.text
    registration = registered.json()
    assert registration["account"]["email"] == "stage.user@example.test"
    assert "password" not in registration and "password_digest" not in registration

    duplicate = client.post("/v1/auth/local/register", json=credentials)
    assert duplicate.status_code == 409

    logged_in = client.post("/v1/auth/local/login", json=credentials)
    assert logged_in.status_code == 200
    token = logged_in.json()["access_token"]
    authorization = {"Authorization": f"Bearer {token}"}

    logged_out = client.post("/v1/auth/local/logout", json={}, headers=authorization)
    assert logged_out.status_code == 200
    assert client.delete("/v1/auth/local/account", headers=authorization).status_code == 401

    second_login = client.post("/v1/auth/local/login", json=credentials)
    assert second_login.status_code == 200
    deleted = client.delete(
        "/v1/auth/local/account",
        headers={"Authorization": f"Bearer {second_login.json()['access_token']}"},
    )
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": True}
    assert client.post("/v1/auth/local/login", json=credentials).status_code == 401

    # Hard deletion removes the credential/contact record and permits a fresh
    # account, while separate research records remain governed by retention.
    assert client.post("/v1/auth/local/register", json=credentials).status_code == 201
    reset_state()


def test_local_auth_is_disabled_without_explicit_staging_mode(monkeypatch) -> None:
    monkeypatch.delenv("FORESIGHTNINE_LOCAL_AUTH_MODE", raising=False)
    response = TestClient(app).post(
        "/v1/auth/local/register",
        json={"email": "disabled@example.test", "password": "correct horse battery staple!"},
    )
    assert response.status_code == 503
    assert response.json()["error_code"] == "internal_error"
