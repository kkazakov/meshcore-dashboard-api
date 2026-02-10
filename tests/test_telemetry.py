"""
Tests for GET /api/telemetry endpoint.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

import app.api.routes.auth as auth_module
from app.main import app

client = TestClient(app)

# ── Fixtures ──────────────────────────────────────────────────────────────────

_VALID_TOKEN = "test-token-abc123"

_SAMPLE_STATUS = {
    "bat": 3900,
    "uptime": 90061,
    "noise_floor": -95,
    "last_rssi": -80,
    "last_snr": 7.5,
    "tx_queue_len": 0,
    "full_evts": 0,
    "nb_sent": 100,
    "sent_flood": 60,
    "sent_direct": 40,
    "nb_recv": 200,
    "recv_flood": 120,
    "recv_direct": 80,
    "direct_dups": 1,
    "flood_dups": 2,
    "airtime": 5000,
    "rx_airtime": 3000,
    "pubkey_pre": "aabbcc001122",
}

_CONTACT = {
    "id": "contact-1",
    "name": "Repeater-Alpha",
    "data": {"adv_name": "Repeater-Alpha", "public_key": "aabbcc001122"},
}


def _install_token():
    """Insert a known token into the in-memory store."""
    auth_module._token_store[_VALID_TOKEN] = "test@example.com"


def _remove_token():
    auth_module._token_store.pop(_VALID_TOKEN, None)


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
    body = response.json()
    assert body["detail"]["status"] == "unauthorized"


# ── Parameter validation ──────────────────────────────────────────────────────


def test_telemetry_no_params_returns_400():
    """Requests with neither repeater_name nor public_key return 400."""
    _install_token()
    try:
        response = client.get(
            "/api/telemetry",
            headers={"x-api-token": _VALID_TOKEN},
        )
        assert response.status_code == 400
        body = response.json()
        assert body["detail"]["status"] == "error"
    finally:
        _remove_token()


# ── Happy path ────────────────────────────────────────────────────────────────


def test_telemetry_by_name_success():
    """Returns 200 with wrapped telemetry data when contact found by name."""
    _install_token()
    try:
        with (
            patch(
                "app.api.routes.telemetry.telemetry_common.connect_to_device",
                new_callable=AsyncMock,
                return_value=MagicMock(),
            ),
            patch(
                "app.api.routes.telemetry.telemetry_common.find_contact_by_name",
                new_callable=AsyncMock,
                return_value=_CONTACT,
            ),
            patch(
                "app.api.routes.telemetry.telemetry_common.get_status",
                new_callable=AsyncMock,
                return_value=_SAMPLE_STATUS,
            ),
        ):
            response = client.get(
                "/api/telemetry?repeater_name=Alpha",
                headers={"x-api-token": _VALID_TOKEN},
            )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        data = body["data"]
        assert data["contact_name"] == "Repeater-Alpha"
        assert data["public_key"] == "aabbcc001122"
        assert data["battery"]["mv"] == 3900
        assert data["battery"]["v"] == 3.9
        assert isinstance(data["battery"]["percentage"], float)
        assert data["uptime"]["days"] == 1
        assert data["radio"]["noise_floor"] == -95
        assert data["packets"]["sent"]["total"] == 100
    finally:
        _remove_token()


def test_telemetry_by_public_key_success():
    """Returns 200 when contact found by public_key (name lookup returns None)."""
    _install_token()
    try:
        with (
            patch(
                "app.api.routes.telemetry.telemetry_common.connect_to_device",
                new_callable=AsyncMock,
                return_value=MagicMock(),
            ),
            patch(
                "app.api.routes.telemetry.telemetry_common.find_contact_by_public_key",
                new_callable=AsyncMock,
                return_value=_CONTACT,
            ),
            patch(
                "app.api.routes.telemetry.telemetry_common.get_status",
                new_callable=AsyncMock,
                return_value=_SAMPLE_STATUS,
            ),
        ):
            response = client.get(
                "/api/telemetry?public_key=aabbcc001122",
                headers={"x-api-token": _VALID_TOKEN},
            )

        assert response.status_code == 200
        assert response.json()["status"] == "ok"
    finally:
        _remove_token()


# ── Error path ────────────────────────────────────────────────────────────────


def test_telemetry_contact_not_found_returns_404():
    """Returns 404 when the contact cannot be located on the device."""
    _install_token()
    try:
        with (
            patch(
                "app.api.routes.telemetry.telemetry_common.connect_to_device",
                new_callable=AsyncMock,
                return_value=MagicMock(),
            ),
            patch(
                "app.api.routes.telemetry.telemetry_common.find_contact_by_name",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            response = client.get(
                "/api/telemetry?repeater_name=Ghost",
                headers={"x-api-token": _VALID_TOKEN},
            )

        assert response.status_code == 404
        assert response.json()["detail"]["status"] == "error"
    finally:
        _remove_token()


def test_telemetry_device_offline_returns_504():
    """Returns 504 when the device does not respond with telemetry."""
    _install_token()
    try:
        with (
            patch(
                "app.api.routes.telemetry.telemetry_common.connect_to_device",
                new_callable=AsyncMock,
                return_value=MagicMock(),
            ),
            patch(
                "app.api.routes.telemetry.telemetry_common.find_contact_by_name",
                new_callable=AsyncMock,
                return_value=_CONTACT,
            ),
            patch(
                "app.api.routes.telemetry.telemetry_common.get_status",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            response = client.get(
                "/api/telemetry?repeater_name=Alpha",
                headers={"x-api-token": _VALID_TOKEN},
            )

        assert response.status_code == 504
        assert response.json()["detail"]["status"] == "error"
    finally:
        _remove_token()


def test_telemetry_connection_failure_returns_502():
    """Returns 502 when the MeshCore device cannot be reached."""
    _install_token()
    try:
        with patch(
            "app.api.routes.telemetry.telemetry_common.connect_to_device",
            new_callable=AsyncMock,
            side_effect=OSError("connection refused"),
        ):
            response = client.get(
                "/api/telemetry?repeater_name=Alpha",
                headers={"x-api-token": _VALID_TOKEN},
            )

        assert response.status_code == 502
        assert response.json()["detail"]["status"] == "error"
    finally:
        _remove_token()
