"""
Tests for POST /api/login and token validation.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import bcrypt
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

_HASH = bcrypt.hashpw(b"secret", bcrypt.gensalt()).decode()

_ACTIVE_ROW = [(_HASH, "alice", True, "")]
_INACTIVE_ROW = [(_HASH, "alice", False, "")]

_VALID_TOKEN = "test-token-auth-abc123"


def _mock_client(rows: list) -> MagicMock:
    """Return a mock ClickHouse client whose query() yields *rows*."""
    mock_result = MagicMock()
    mock_result.result_rows = rows
    mock_ch = MagicMock()
    mock_ch.query.return_value = mock_result
    return mock_ch


def test_login_success():
    """Returns 200 and user details when credentials are valid."""
    mock_result = MagicMock()
    mock_result.type = MagicMock()
    mock_result.payload = {"name": "test-device"}

    mock_meshcore = MagicMock()
    mock_meshcore.commands.send_appstart = AsyncMock(return_value=mock_result)
    mock_meshcore.disconnect = AsyncMock()

    with (
        patch("app.api.routes.auth.get_client", return_value=_mock_client(_ACTIVE_ROW)),
        patch(
            "app.api.routes.auth.telemetry_common.connect_to_device",
            new_callable=AsyncMock,
            return_value=mock_meshcore,
        ),
    ):
        response = client.post(
            "/api/login", json={"email": "alice@example.com", "password": "secret"}
        )

    assert response.status_code == 200
    body = response.json()
    assert body["email"] == "alice@example.com"
    assert body["username"] == "alice"
    assert body["access_rights"] == ""
    assert body["device_name"] == "test-device"
    assert isinstance(body["token"], str) and len(body["token"]) == 64


def test_login_success_stores_token():
    """Each successful login mints a unique token stored in ClickHouse."""
    inserted_tokens: list[str] = []

    def capture_insert(table, data, column_names=None):
        if data and isinstance(data, list):
            for row in data:
                if row and len(row) > 0:
                    inserted_tokens.append(row[0])
        return None

    mock_ch = _mock_client(_ACTIVE_ROW)
    mock_ch.insert = MagicMock(side_effect=capture_insert)

    mock_result = MagicMock()
    mock_result.type = MagicMock()
    mock_result.payload = {"name": "test-device"}

    mock_meshcore = MagicMock()
    mock_meshcore.commands.send_appstart = AsyncMock(return_value=mock_result)
    mock_meshcore.disconnect = AsyncMock()

    with (
        patch("app.api.routes.auth.get_client", return_value=mock_ch),
        patch(
            "app.api.routes.auth.telemetry_common.connect_to_device",
            new_callable=AsyncMock,
            return_value=mock_meshcore,
        ),
    ):
        r1 = client.post(
            "/api/login", json={"email": "alice@example.com", "password": "secret"}
        )
        r2 = client.post(
            "/api/login", json={"email": "alice@example.com", "password": "secret"}
        )

    t1 = r1.json()["token"]
    t2 = r2.json()["token"]
    assert t1 != t2
    assert t1 in inserted_tokens
    assert t2 in inserted_tokens


def test_login_wrong_password():
    """Returns 401 when the password does not match the stored hash."""
    with patch(
        "app.api.routes.auth.get_client", return_value=_mock_client(_ACTIVE_ROW)
    ):
        response = client.post(
            "/api/login", json={"email": "alice@example.com", "password": "wrong"}
        )

    assert response.status_code == 401


def test_login_unknown_user():
    """Returns 401 when no matching user is found."""
    with patch("app.api.routes.auth.get_client", return_value=_mock_client([])):
        response = client.post(
            "/api/login", json={"email": "nobody@example.com", "password": "x"}
        )

    assert response.status_code == 401


def test_login_inactive_account():
    """Returns 401 when the account is not active."""
    with patch(
        "app.api.routes.auth.get_client", return_value=_mock_client(_INACTIVE_ROW)
    ):
        response = client.post(
            "/api/login", json={"email": "alice@example.com", "password": "secret"}
        )

    assert response.status_code == 401
    assert "inactive" in response.json()["detail"].lower()


def test_login_database_unavailable():
    """Returns 503 when ClickHouse raises an exception."""
    mock_ch = MagicMock()
    mock_ch.query.side_effect = Exception("connection refused")
    with patch("app.api.routes.auth.get_client", return_value=mock_ch):
        response = client.post(
            "/api/login", json={"email": "alice@example.com", "password": "secret"}
        )

    assert response.status_code == 503


# ---------------------------------------------------------------------------
# Token validation respects active flag
# ---------------------------------------------------------------------------


def _mock_deps_client(active: bool) -> MagicMock:
    """
    Return a mock ClickHouse client for ``app.api.deps``.

    When *active* is True the JOIN query returns a row (user is active and
    the token is valid).  When False it returns no rows, simulating a
    deactivated or deleted user.
    """
    mock_result = MagicMock()
    mock_result.result_rows = [["alice@example.com"]] if active else []
    mock_ch = MagicMock()
    mock_ch.query.return_value = mock_result
    return mock_ch


def test_require_token_rejects_inactive_user():
    """
    A token whose user has active=false must be rejected with 401.

    The token JOIN query returns no rows when the user is inactive, so
    ``require_token`` must raise HTTP 401 rather than returning the email.
    """
    mock_ch = _mock_deps_client(active=False)
    # Use any protected endpoint — repeaters list is simple and dependency-free.
    with patch("app.api.deps.get_client", return_value=mock_ch):
        response = client.get("/api/repeaters", headers={"x-api-token": _VALID_TOKEN})

    assert response.status_code == 401


def test_require_token_accepts_active_user():
    """
    A token belonging to an active user must be accepted (returns the email).

    We verify this by checking that the protected endpoint does NOT return 401.
    The repeaters query itself is mocked to return an empty list so we only
    need to stub ``deps.get_client`` for the token check and
    ``repeaters.get_client`` for the list query.
    """
    token_ch = _mock_deps_client(active=True)

    # Stub the repeaters list query
    repeater_result = MagicMock()
    repeater_result.result_rows = []
    repeater_ch = MagicMock()
    repeater_ch.query.return_value = repeater_result

    with (
        patch("app.api.deps.get_client", return_value=token_ch),
        patch("app.api.routes.repeaters.get_client", return_value=repeater_ch),
    ):
        response = client.get("/api/repeaters", headers={"x-api-token": _VALID_TOKEN})

    assert response.status_code == 200


def test_status_not_authenticated_for_inactive_user_token():
    """
    GET /status must return authenticated=false when the token owner is inactive.

    ``_check_token_valid`` now JOINs users and filters active=true, so a token
    for a deactivated user returns no rows and therefore authenticated=false.
    """
    mock_result = MagicMock()
    mock_result.result_rows = []  # no rows → inactive / deleted user
    mock_ch = MagicMock()
    mock_ch.query.return_value = mock_result

    with (
        patch("app.api.routes.status.ping", return_value=(True, 1.0)),
        patch("app.api.routes.status.get_client", return_value=mock_ch),
    ):
        response = client.get("/status", headers={"x-api-token": _VALID_TOKEN})

    assert response.status_code == 200
    assert response.json()["authenticated"] is False
