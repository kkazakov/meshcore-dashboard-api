"""
Tests for GET /status endpoint.
"""

from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_status_ok():
    """Returns 200 with status=ok when ClickHouse is reachable."""
    with patch("app.api.routes.status.ping", return_value=(True, 1.23)):
        response = client.get("/status")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["clickhouse"]["connected"] is True
    assert body["clickhouse"]["latency_ms"] == 1.23
    assert body["authenticated"] is False


def test_status_degraded():
    """Returns 200 with status=degraded when ClickHouse is unreachable."""
    with patch("app.api.routes.status.ping", return_value=(False, 500.0)):
        response = client.get("/status")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["clickhouse"]["connected"] is False
    assert body["authenticated"] is False


def test_status_authenticated_with_valid_token():
    """Returns authenticated=true when a valid session token is supplied."""
    fake_store = {"validtoken123": "user@example.com"}
    with (
        patch("app.api.routes.status.ping", return_value=(True, 1.0)),
        patch("app.api.routes.status._token_store", fake_store),
    ):
        response = client.get("/status", headers={"x-api-token": "validtoken123"})

    assert response.status_code == 200
    assert response.json()["authenticated"] is True


def test_status_not_authenticated_with_wrong_token():
    """Returns authenticated=false when the token is not in the store."""
    fake_store = {"validtoken123": "user@example.com"}
    with (
        patch("app.api.routes.status.ping", return_value=(True, 1.0)),
        patch("app.api.routes.status._token_store", fake_store),
    ):
        response = client.get("/status", headers={"x-api-token": "wrongtoken"})

    assert response.status_code == 200
    assert response.json()["authenticated"] is False


def test_status_not_authenticated_without_token():
    """Returns authenticated=false when no x-api-token header is provided."""
    with patch("app.api.routes.status.ping", return_value=(True, 1.0)):
        response = client.get("/status")

    assert response.status_code == 200
    assert response.json()["authenticated"] is False
