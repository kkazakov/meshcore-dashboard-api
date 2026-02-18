"""
Tests for PATCH /api/repeaters/{repeater_id} (update repeater).
"""

import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, call, patch

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

_VALID_TOKEN = "test-token-abc"
_REPEATER_ID = str(uuid.uuid4())
_OTHER_ID = str(uuid.uuid4())
_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _auth_client() -> MagicMock:
    """Mock ClickHouse client for app.api.deps (token validation)."""
    result = MagicMock()
    result.result_rows = [["test@example.com"]]
    ch = MagicMock()
    ch.query.return_value = result
    return ch


def _repeater_client(
    existing_row: list | None, conflict_row: list | None = None
) -> MagicMock:
    """
    Mock ClickHouse client for the repeaters route.

    *existing_row* – the row returned for the lookup by id (None → not found).
    *conflict_row* – the row returned for the public_key uniqueness check
                     (None → no conflict).
    """
    ch = MagicMock()

    def _query(sql: str, parameters: dict | None = None) -> MagicMock:
        result = MagicMock()
        if "WHERE id" in sql:
            result.result_rows = [existing_row] if existing_row is not None else []
        elif "WHERE public_key" in sql:
            result.result_rows = [conflict_row] if conflict_row is not None else []
        else:
            result.result_rows = []
        return result

    ch.query.side_effect = _query
    return ch


# ---------------------------------------------------------------------------
# 401 / auth tests
# ---------------------------------------------------------------------------


def test_update_repeater_missing_token_returns_401():
    """PATCH without x-api-token must return 401."""
    response = client.patch(f"/api/repeaters/{_REPEATER_ID}", json={"name": "new-name"})
    assert response.status_code == 401


def test_update_repeater_invalid_token_returns_401():
    """PATCH with an unrecognised token must return 401."""
    bad_auth = MagicMock()
    bad_auth.query.return_value = MagicMock(result_rows=[])
    with patch("app.api.deps.get_client", return_value=bad_auth):
        response = client.patch(
            f"/api/repeaters/{_REPEATER_ID}",
            json={"name": "new-name"},
            headers={"x-api-token": "bad-token"},
        )
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# 400 – validation errors
# ---------------------------------------------------------------------------


def test_update_repeater_invalid_uuid_returns_400():
    """A non-UUID repeater_id path parameter must return 400."""
    auth = _auth_client()
    with patch("app.api.deps.get_client", return_value=auth):
        response = client.patch(
            "/api/repeaters/not-a-uuid",
            json={"name": "new-name"},
            headers={"x-api-token": _VALID_TOKEN},
        )
    assert response.status_code == 400
    assert "Invalid repeater ID format" in response.json()["detail"]


def test_update_repeater_no_fields_returns_400():
    """Sending an empty body (no name, no public_key) must return 400."""
    auth = _auth_client()
    with patch("app.api.deps.get_client", return_value=auth):
        response = client.patch(
            f"/api/repeaters/{_REPEATER_ID}",
            json={},
            headers={"x-api-token": _VALID_TOKEN},
        )
    assert response.status_code == 400
    assert "name or public_key" in response.json()["detail"]


def test_update_repeater_blank_name_returns_400():
    """Sending a blank name string must return 400."""
    auth = _auth_client()
    with patch("app.api.deps.get_client", return_value=auth):
        response = client.patch(
            f"/api/repeaters/{_REPEATER_ID}",
            json={"name": "   "},
            headers={"x-api-token": _VALID_TOKEN},
        )
    assert response.status_code == 400
    assert "name" in response.json()["detail"]


def test_update_repeater_blank_public_key_returns_400():
    """Sending a blank public_key string must return 400."""
    auth = _auth_client()
    with patch("app.api.deps.get_client", return_value=auth):
        response = client.patch(
            f"/api/repeaters/{_REPEATER_ID}",
            json={"public_key": "   "},
            headers={"x-api-token": _VALID_TOKEN},
        )
    assert response.status_code == 400
    assert "public_key" in response.json()["detail"]


# ---------------------------------------------------------------------------
# 404 – repeater not found
# ---------------------------------------------------------------------------


def test_update_repeater_not_found_returns_404():
    """Updating a repeater that doesn't exist must return 404."""
    auth = _auth_client()
    repeater_ch = _repeater_client(existing_row=None)
    with (
        patch("app.api.deps.get_client", return_value=auth),
        patch("app.api.routes.repeaters.get_client", return_value=repeater_ch),
    ):
        response = client.patch(
            f"/api/repeaters/{_REPEATER_ID}",
            json={"name": "new-name"},
            headers={"x-api-token": _VALID_TOKEN},
        )
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


# ---------------------------------------------------------------------------
# 409 – public_key conflict
# ---------------------------------------------------------------------------


def test_update_repeater_duplicate_public_key_returns_409():
    """Changing to a public_key already used by another repeater must return 409."""
    auth = _auth_client()
    existing_row = [_REPEATER_ID, "old-name", "old-key", "", True]
    conflict_row = [_OTHER_ID]  # another repeater owns the new key
    repeater_ch = _repeater_client(existing_row=existing_row, conflict_row=conflict_row)
    with (
        patch("app.api.deps.get_client", return_value=auth),
        patch("app.api.routes.repeaters.get_client", return_value=repeater_ch),
    ):
        response = client.patch(
            f"/api/repeaters/{_REPEATER_ID}",
            json={"public_key": "taken-key"},
            headers={"x-api-token": _VALID_TOKEN},
        )
    assert response.status_code == 409
    assert "already exists" in response.json()["detail"]


# ---------------------------------------------------------------------------
# 200 – successful updates
# ---------------------------------------------------------------------------


def test_update_repeater_name_only_returns_200():
    """Updating only the name must succeed and return the updated repeater."""
    auth = _auth_client()
    existing_row = [_REPEATER_ID, "old-name", "existing-key", "secret", True]
    repeater_ch = _repeater_client(existing_row=existing_row)
    with (
        patch("app.api.deps.get_client", return_value=auth),
        patch("app.api.routes.repeaters.get_client", return_value=repeater_ch),
    ):
        response = client.patch(
            f"/api/repeaters/{_REPEATER_ID}",
            json={"name": "new-name"},
            headers={"x-api-token": _VALID_TOKEN},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["repeater"]["name"] == "new-name"
    assert body["repeater"]["public_key"] == "existing-key"
    assert body["repeater"]["id"] == _REPEATER_ID
    # No public_key conflict check should have been performed
    repeater_ch.query.assert_called_once()


def test_update_repeater_public_key_only_returns_200():
    """Updating only the public_key must succeed and check for conflicts."""
    auth = _auth_client()
    existing_row = [_REPEATER_ID, "my-repeater", "old-key", "", False]
    repeater_ch = _repeater_client(existing_row=existing_row, conflict_row=None)
    with (
        patch("app.api.deps.get_client", return_value=auth),
        patch("app.api.routes.repeaters.get_client", return_value=repeater_ch),
    ):
        response = client.patch(
            f"/api/repeaters/{_REPEATER_ID}",
            json={"public_key": "new-key"},
            headers={"x-api-token": _VALID_TOKEN},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["repeater"]["public_key"] == "new-key"
    assert body["repeater"]["name"] == "my-repeater"
    assert body["repeater"]["enabled"] is False
    # Two queries: id lookup + public_key conflict check
    assert repeater_ch.query.call_count == 2


def test_update_repeater_both_fields_returns_200():
    """Updating both name and public_key at once must succeed."""
    auth = _auth_client()
    existing_row = [_REPEATER_ID, "old-name", "old-key", "pw", True]
    repeater_ch = _repeater_client(existing_row=existing_row, conflict_row=None)
    with (
        patch("app.api.deps.get_client", return_value=auth),
        patch("app.api.routes.repeaters.get_client", return_value=repeater_ch),
    ):
        response = client.patch(
            f"/api/repeaters/{_REPEATER_ID}",
            json={"name": "  new-name  ", "public_key": "  new-key  "},
            headers={"x-api-token": _VALID_TOKEN},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["repeater"]["name"] == "new-name"
    assert body["repeater"]["public_key"] == "new-key"


def test_update_repeater_same_public_key_skips_conflict_check():
    """
    If public_key is unchanged the uniqueness check must NOT be performed
    (no second query to the database).
    """
    auth = _auth_client()
    existing_row = [_REPEATER_ID, "old-name", "same-key", "", True]
    repeater_ch = _repeater_client(existing_row=existing_row)
    with (
        patch("app.api.deps.get_client", return_value=auth),
        patch("app.api.routes.repeaters.get_client", return_value=repeater_ch),
    ):
        response = client.patch(
            f"/api/repeaters/{_REPEATER_ID}",
            json={"name": "new-name", "public_key": "same-key"},
            headers={"x-api-token": _VALID_TOKEN},
        )

    assert response.status_code == 200
    # Only the id-lookup query, no conflict-check query
    repeater_ch.query.assert_called_once()


def test_update_repeater_preserves_enabled_and_password():
    """The enabled flag and password must be preserved from the existing record."""
    auth = _auth_client()
    existing_row = [_REPEATER_ID, "r1", "key1", "hunter2", False]
    repeater_ch = _repeater_client(existing_row=existing_row)
    with (
        patch("app.api.deps.get_client", return_value=auth),
        patch("app.api.routes.repeaters.get_client", return_value=repeater_ch),
    ):
        response = client.patch(
            f"/api/repeaters/{_REPEATER_ID}",
            json={"name": "r1-updated"},
            headers={"x-api-token": _VALID_TOKEN},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["repeater"]["enabled"] is False

    # Verify the insert preserved the password and enabled flag
    insert_call: call = repeater_ch.insert.call_args
    inserted_row = insert_call[0][1][0]  # positional arg: list of rows → first row
    # columns: id, name, public_key, password, enabled, created_at
    assert inserted_row[3] == "hunter2"  # password preserved
    assert inserted_row[4] is False  # enabled preserved
