"""
Tests for GET /api/telemetry endpoint.
"""

from unittest.mock import AsyncMock, MagicMock, patch
from contextlib import contextmanager

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

_VALID_TOKEN = "test-token-abc123"


def _mock_token_client() -> MagicMock:
    """Create a mock ClickHouse client that validates _VALID_TOKEN."""
    mock_result = MagicMock()
    mock_result.result_rows = [["test@example.com"]]
    mock_ch = MagicMock()
    mock_ch.query.return_value = mock_result
    return mock_ch


@contextmanager
def _valid_token():
    """Context manager that provides a valid token for authenticated requests."""
    mock_client = _mock_token_client()
    with patch("app.api.deps.get_client", return_value=mock_client):
        yield _VALID_TOKEN


# ── Auth guard tests ──────────────────────────────────────────────────────────


def test_telemetry_missing_token_returns_401():
    """Requests without x-api-token header are rejected with 401."""
    response = client.get("/api/telemetry?repeater_name=Alpha")
    assert response.status_code == 401


def test_telemetry_invalid_token_returns_401():
    """Requests with an unknown token are rejected with 401."""
    response = client.get(
        "/api/telemetry?repeater_name=Alpha",
        headers={"x-api-token": "not-a-real-token"},
    )
    assert response.status_code == 401
    assert response.json()["detail"]["status"] == "unauthorized"


def test_telemetry_connection_failure_returns_502():
    """Returns 502 when connection to the device fails."""
    with (
        _valid_token(),
        patch("app.api.routes.telemetry.telemetry_common.load_config", return_value={}),
        patch(
            "app.api.routes.telemetry.telemetry_common.connect_to_device",
            new=AsyncMock(side_effect=OSError("device not found")),
        ),
    ):
        response = client.get(
            "/api/telemetry?repeater_name=Alpha",
            headers={"x-api-token": _VALID_TOKEN},
        )

    assert response.status_code == 502


def test_telemetry_neither_name_nor_key_returns_400():
    """Returns 400 when neither repeater_name nor public_key is provided."""
    with _valid_token():
        response = client.get(
            "/api/telemetry",
            headers={"x-api-token": _VALID_TOKEN},
        )

    assert response.status_code == 400


# ── Live telemetry happy-path tests ──────────────────────────────────────────


def test_telemetry_returns_sensors_when_available():
    """Sensor data (temperature, humidity, pressure) appears under 'sensors' key."""
    fake_status = {
        "bat": 4000,
        "uptime": 3600,
        "noise_floor": -90,
        "last_rssi": -60,
        "last_snr": 8,
        "tx_queue_len": 0,
        "full_evts": 0,
        "nb_sent": 5,
        "sent_flood": 3,
        "sent_direct": 2,
        "nb_recv": 10,
        "recv_flood": 7,
        "recv_direct": 3,
        "direct_dups": 0,
        "flood_dups": 1,
        "airtime": 5,
        "rx_airtime": 12,
        "pubkey_pre": "aabbcc112233",
        "public_key": "aabbcc1122334455",
    }
    fake_sensors = {
        "temperature_c": 22.5,
        "humidity_pct": 55.0,
        "pressure_hpa": 1013.25,
    }
    fake_contact = {
        "id": "contact-1",
        "name": "TestRepeater",
        "data": {"public_key": "aabbcc1122334455"},
    }
    mock_meshcore = MagicMock()
    mock_meshcore.disconnect = AsyncMock()

    with (
        _valid_token(),
        patch("app.api.routes.telemetry.telemetry_common.load_config", return_value={}),
        patch(
            "app.api.routes.telemetry.telemetry_common.connect_to_device",
            new=AsyncMock(return_value=mock_meshcore),
        ),
        patch(
            "app.api.routes.telemetry.telemetry_common.find_contact_by_name",
            new=AsyncMock(return_value=fake_contact),
        ),
        patch(
            "app.api.routes.telemetry.telemetry_common.get_status",
            new=AsyncMock(return_value=fake_status),
        ),
        patch(
            "app.api.routes.telemetry.telemetry_common.get_sensor_telemetry",
            new=AsyncMock(return_value=fake_sensors),
        ),
    ):
        response = client.get(
            "/api/telemetry?repeater_name=TestRepeater",
            headers={"x-api-token": _VALID_TOKEN},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    sensors = body["data"]["sensors"]
    assert sensors["temperature_c"] == 22.5
    assert sensors["humidity_pct"] == 55.0
    assert sensors["pressure_hpa"] == 1013.25


def test_telemetry_sensors_null_when_unavailable():
    """'sensors' key is null when device does not support sensor telemetry."""
    fake_status = {
        "bat": 4000,
        "uptime": 3600,
        "noise_floor": -90,
        "last_rssi": -60,
        "last_snr": 8,
        "tx_queue_len": 0,
        "full_evts": 0,
        "nb_sent": 5,
        "sent_flood": 3,
        "sent_direct": 2,
        "nb_recv": 10,
        "recv_flood": 7,
        "recv_direct": 3,
        "direct_dups": 0,
        "flood_dups": 1,
        "airtime": 5,
        "rx_airtime": 12,
        "pubkey_pre": "aabbcc112233",
        "public_key": "aabbcc1122334455",
    }
    fake_contact = {
        "id": "contact-1",
        "name": "TestRepeater",
        "data": {"public_key": "aabbcc1122334455"},
    }
    mock_meshcore = MagicMock()
    mock_meshcore.disconnect = AsyncMock()

    with (
        _valid_token(),
        patch("app.api.routes.telemetry.telemetry_common.load_config", return_value={}),
        patch(
            "app.api.routes.telemetry.telemetry_common.connect_to_device",
            new=AsyncMock(return_value=mock_meshcore),
        ),
        patch(
            "app.api.routes.telemetry.telemetry_common.find_contact_by_name",
            new=AsyncMock(return_value=fake_contact),
        ),
        patch(
            "app.api.routes.telemetry.telemetry_common.get_status",
            new=AsyncMock(return_value=fake_status),
        ),
        patch(
            "app.api.routes.telemetry.telemetry_common.get_sensor_telemetry",
            new=AsyncMock(return_value=None),  # device does not support it
        ),
    ):
        response = client.get(
            "/api/telemetry?repeater_name=TestRepeater",
            headers={"x-api-token": _VALID_TOKEN},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["data"]["sensors"] is None


# ── Telemetry history tests ───────────────────────────────────────────────────


def test_telemetry_history_missing_token_returns_401():
    """GET /api/telemetry/history without x-api-token returns 401."""
    response = client.get("/api/telemetry/history/some-id")
    assert response.status_code == 401


def test_telemetry_history_invalid_token_returns_401():
    """GET /api/telemetry/history with an unknown token returns 401."""
    response = client.get(
        "/api/telemetry/history/some-id",
        headers={"x-api-token": "bad-token"},
    )
    assert response.status_code == 401
    assert response.json()["detail"]["status"] == "unauthorized"


def test_telemetry_history_clickhouse_error_returns_503():
    """Returns 503 when ClickHouse query fails."""
    with (
        _valid_token(),
        patch("app.api.routes.telemetry.get_client") as mock_get_client,
    ):
        mock_get_client.return_value.query.side_effect = Exception("connection refused")
        response = client.get(
            "/api/telemetry/history/aabbcc001122?keys=battery_voltage",
            headers={"x-api-token": _VALID_TOKEN},
        )

    assert response.status_code == 503
