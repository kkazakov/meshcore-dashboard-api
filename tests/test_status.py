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


def test_status_degraded():
    """Returns 200 with status=degraded when ClickHouse is unreachable."""
    with patch("app.api.routes.status.ping", return_value=(False, 500.0)):
        response = client.get("/status")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["clickhouse"]["connected"] is False
