"""
Tests for POST /api/messages and GET /api/messages endpoints.
"""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

import app.api.routes.auth as auth_module
from app.main import app
from meshcore import EventType

client = TestClient(app)

# ── Fixtures ──────────────────────────────────────────────────────────────────

_VALID_TOKEN = "test-token-messages-xyz"


def _install_token() -> None:
    auth_module._token_store[_VALID_TOKEN] = "test@example.com"


def _remove_token() -> None:
    auth_module._token_store.pop(_VALID_TOKEN, None)


def _make_channel_event(idx: int, name: str) -> MagicMock:
    """Build a fake get_channel() Event with the given slot data."""
    evt = MagicMock()
    evt.type = EventType.OK
    evt.payload = {
        "channel_idx": idx,
        "channel_name": name,
        "channel_secret": bytes([0xAB] * 16),
    }
    return evt


def _make_ok_event() -> MagicMock:
    evt = MagicMock()
    evt.type = EventType.OK
    return evt


def _make_error_event(reason: str = "rejected") -> MagicMock:
    evt = MagicMock()
    evt.type = EventType.ERROR
    evt.payload = reason
    return evt


def _build_meshcore_mock(channels: list[tuple[int, str]]) -> MagicMock:
    """
    Return a MeshCore mock whose get_channel() returns the given channels,
    followed by an ERROR event (end-of-list sentinel).
    """
    meshcore = MagicMock()

    channel_events = [_make_channel_event(idx, name) for idx, name in channels]
    # ERROR terminates the scan loop in _resolve_channel_index
    channel_events.append(_make_error_event())

    meshcore.commands.get_channel = AsyncMock(side_effect=channel_events)
    meshcore.commands.send_chan_msg = AsyncMock(return_value=_make_ok_event())
    meshcore.disconnect = AsyncMock()
    return meshcore


# ── Auth guard tests ──────────────────────────────────────────────────────────


def test_send_message_missing_token_returns_401():
    """Requests without x-api-token are rejected with 401."""
    response = client.post(
        "/api/messages", json={"channel": "#test", "message": "hello"}
    )
    assert response.status_code == 401


def test_send_message_invalid_token_returns_401():
    """Requests with an unknown token are rejected with 401."""
    response = client.post(
        "/api/messages",
        json={"channel": "#test", "message": "hello"},
        headers={"x-api-token": "bad-token"},
    )
    assert response.status_code == 401
    assert response.json()["detail"]["status"] == "unauthorized"


# ── Input validation ──────────────────────────────────────────────────────────


def test_send_message_empty_channel_returns_400():
    _install_token()
    try:
        response = client.post(
            "/api/messages",
            json={"channel": "", "message": "hello"},
            headers={"x-api-token": _VALID_TOKEN},
        )
        assert response.status_code == 400
        assert "channel" in response.json()["detail"]["message"]
    finally:
        _remove_token()


def test_send_message_empty_message_returns_400():
    _install_token()
    try:
        response = client.post(
            "/api/messages",
            json={"channel": "#test", "message": ""},
            headers={"x-api-token": _VALID_TOKEN},
        )
        assert response.status_code == 400
        assert "message" in response.json()["detail"]["message"]
    finally:
        _remove_token()


# ── Happy path ────────────────────────────────────────────────────────────────


def test_send_message_success_with_hash_prefix():
    """``#test`` resolves to the 'test' channel and returns 200."""
    _install_token()
    meshcore_mock = _build_meshcore_mock([(0, "test"), (1, "general")])

    with (
        patch(
            "app.api.routes.messages.telemetry_common.load_config",
            return_value={},
        ),
        patch(
            "app.api.routes.messages.telemetry_common.connect_to_device",
            new=AsyncMock(return_value=meshcore_mock),
        ),
    ):
        try:
            response = client.post(
                "/api/messages",
                json={"channel": "#test", "message": "Проба 123"},
                headers={"x-api-token": _VALID_TOKEN},
            )
        finally:
            _remove_token()

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["channel_name"] == "test"
    assert body["channel_index"] == 0
    # Verify the library call used the right slot index and the full text.
    meshcore_mock.commands.send_chan_msg.assert_awaited_once_with(0, "Проба 123")


def test_send_message_success_without_hash_prefix():
    """``test`` (no ``#``) also resolves correctly."""
    _install_token()
    meshcore_mock = _build_meshcore_mock([(0, "test")])

    with (
        patch(
            "app.api.routes.messages.telemetry_common.load_config",
            return_value={},
        ),
        patch(
            "app.api.routes.messages.telemetry_common.connect_to_device",
            new=AsyncMock(return_value=meshcore_mock),
        ),
    ):
        try:
            response = client.post(
                "/api/messages",
                json={"channel": "test", "message": "hello"},
                headers={"x-api-token": _VALID_TOKEN},
            )
        finally:
            _remove_token()

    assert response.status_code == 200
    body = response.json()
    assert body["channel_name"] == "test"


def test_send_message_channel_stored_with_hash():
    """Device channel named ``#test`` is found when requesting ``#test``."""
    _install_token()
    meshcore_mock = _build_meshcore_mock([(1, "#test")])

    with (
        patch(
            "app.api.routes.messages.telemetry_common.load_config",
            return_value={},
        ),
        patch(
            "app.api.routes.messages.telemetry_common.connect_to_device",
            new=AsyncMock(return_value=meshcore_mock),
        ),
    ):
        try:
            response = client.post(
                "/api/messages",
                json={"channel": "#test", "message": "Проба 123"},
                headers={"x-api-token": _VALID_TOKEN},
            )
        finally:
            _remove_token()

    assert response.status_code == 200
    body = response.json()
    assert body["channel_name"] == "#test"
    assert body["channel_index"] == 1
    meshcore_mock.commands.send_chan_msg.assert_awaited_once_with(1, "Проба 123")


def test_send_message_channel_name_case_insensitive():
    """Channel matching is case-insensitive: ``#TEST`` finds ``test``."""
    _install_token()
    meshcore_mock = _build_meshcore_mock([(2, "test")])

    with (
        patch(
            "app.api.routes.messages.telemetry_common.load_config",
            return_value={},
        ),
        patch(
            "app.api.routes.messages.telemetry_common.connect_to_device",
            new=AsyncMock(return_value=meshcore_mock),
        ),
    ):
        try:
            response = client.post(
                "/api/messages",
                json={"channel": "#TEST", "message": "hi"},
                headers={"x-api-token": _VALID_TOKEN},
            )
        finally:
            _remove_token()

    assert response.status_code == 200
    assert response.json()["channel_index"] == 2


# ── Error paths ───────────────────────────────────────────────────────────────


def test_send_message_channel_not_found_returns_404():
    """Requesting a channel that does not exist on the device returns 404."""
    _install_token()
    # Device has no channels (immediate ERROR from get_channel).
    meshcore_mock = MagicMock()
    meshcore_mock.commands.get_channel = AsyncMock(return_value=_make_error_event())
    meshcore_mock.disconnect = AsyncMock()

    with (
        patch(
            "app.api.routes.messages.telemetry_common.load_config",
            return_value={},
        ),
        patch(
            "app.api.routes.messages.telemetry_common.connect_to_device",
            new=AsyncMock(return_value=meshcore_mock),
        ),
    ):
        try:
            response = client.post(
                "/api/messages",
                json={"channel": "#ghost", "message": "anyone?"},
                headers={"x-api-token": _VALID_TOKEN},
            )
        finally:
            _remove_token()

    assert response.status_code == 404
    assert "ghost" in response.json()["detail"]["message"]


def test_send_message_device_connection_failure_returns_502():
    """A connection error to the MeshCore device returns 502."""
    _install_token()

    with (
        patch(
            "app.api.routes.messages.telemetry_common.load_config",
            return_value={},
        ),
        patch(
            "app.api.routes.messages.telemetry_common.connect_to_device",
            new=AsyncMock(side_effect=OSError("device not found")),
        ),
    ):
        try:
            response = client.post(
                "/api/messages",
                json={"channel": "#test", "message": "hello"},
                headers={"x-api-token": _VALID_TOKEN},
            )
        finally:
            _remove_token()

    assert response.status_code == 502
    assert "Device connection failed" in response.json()["detail"]["message"]


def test_send_message_device_rejects_send_returns_502():
    """If send_chan_msg returns an ERROR event, the endpoint returns 502."""
    _install_token()
    meshcore_mock = _build_meshcore_mock([(0, "test")])
    meshcore_mock.commands.send_chan_msg = AsyncMock(
        return_value=_make_error_event("firmware error")
    )

    with (
        patch(
            "app.api.routes.messages.telemetry_common.load_config",
            return_value={},
        ),
        patch(
            "app.api.routes.messages.telemetry_common.connect_to_device",
            new=AsyncMock(return_value=meshcore_mock),
        ),
    ):
        try:
            response = client.post(
                "/api/messages",
                json={"channel": "#test", "message": "hello"},
                headers={"x-api-token": _VALID_TOKEN},
            )
        finally:
            _remove_token()

    assert response.status_code == 502
    assert "rejected" in response.json()["detail"]["message"]


def test_send_message_device_send_timeout_returns_504():
    """If send_chan_msg times out, the endpoint returns 504."""
    _install_token()
    meshcore_mock = _build_meshcore_mock([(0, "test")])

    async def _slow(*_args, **_kwargs):
        await asyncio.sleep(999)

    meshcore_mock.commands.send_chan_msg = _slow

    with (
        patch(
            "app.api.routes.messages.telemetry_common.load_config",
            return_value={},
        ),
        patch(
            "app.api.routes.messages.telemetry_common.connect_to_device",
            new=AsyncMock(return_value=meshcore_mock),
        ),
        # Shrink the timeout so the test doesn't actually sleep 10 s.
        patch(
            "app.api.routes.messages.asyncio.wait_for", side_effect=asyncio.TimeoutError
        ),
    ):
        try:
            response = client.post(
                "/api/messages",
                json={"channel": "#test", "message": "hello"},
                headers={"x-api-token": _VALID_TOKEN},
            )
        finally:
            _remove_token()

    assert response.status_code == 504
    assert "timeout" in response.json()["detail"]["message"].lower()


# ═══════════════════════════════════════════════════════════════════════════════
# GET /api/messages
# ═══════════════════════════════════════════════════════════════════════════════

# Column order must match _COLUMNS in app/api/routes/messages.py
_COLUMNS = (
    "received_at",
    "sender_name",
    "path_len",
    "text",
)


def _make_row(
    text: str = "hello",
    sender_name: str = "Alice",
    received_at: datetime | None = None,
    path_len: int = 1,
) -> tuple:
    """Return a fake ClickHouse result row with sensible defaults."""
    if received_at is None:
        received_at = datetime(2026, 2, 10, 18, 59, 7, 541000, tzinfo=timezone.utc)
    return (
        received_at,  # received_at → ts
        sender_name,  # sender_name → sender
        path_len,  # path_len
        text,  # text
    )


def _make_ch_result(rows: list[tuple]) -> MagicMock:
    """Build a mock ClickHouse query result."""
    result = MagicMock()
    result.result_rows = rows
    return result


# ── Auth guard ────────────────────────────────────────────────────────────────


def test_get_messages_missing_token_returns_401():
    """GET /api/messages without x-api-token returns 401."""
    response = client.get("/api/messages", params={"channel": "Public"})
    assert response.status_code == 401


def test_get_messages_invalid_token_returns_401():
    """GET /api/messages with an unknown token returns 401."""
    response = client.get(
        "/api/messages",
        params={"channel": "Public"},
        headers={"x-api-token": "bad-token"},
    )
    assert response.status_code == 401
    assert response.json()["detail"]["status"] == "unauthorized"


# ── Input validation ──────────────────────────────────────────────────────────


def test_get_messages_missing_channel_returns_422():
    """channel is a required query parameter; omitting it returns 422."""
    _install_token()
    try:
        response = client.get(
            "/api/messages",
            headers={"x-api-token": _VALID_TOKEN},
        )
        assert response.status_code == 422
    finally:
        _remove_token()


def test_get_messages_empty_channel_returns_400():
    """An empty channel string returns 400."""
    _install_token()
    try:
        response = client.get(
            "/api/messages",
            params={"channel": ""},
            headers={"x-api-token": _VALID_TOKEN},
        )
        assert response.status_code == 400
        assert "channel" in response.json()["detail"]["message"]
    finally:
        _remove_token()


def test_get_messages_from_and_since_mutually_exclusive_returns_400():
    """Supplying both 'from' and 'since' returns 400."""
    _install_token()
    try:
        response = client.get(
            "/api/messages",
            params={
                "channel": "Public",
                "from": 5,
                "since": "2026-02-10 18:59:07.541",
            },
            headers={"x-api-token": _VALID_TOKEN},
        )
        assert response.status_code == 400
        assert "mutually exclusive" in response.json()["detail"]["message"]
    finally:
        _remove_token()


def test_get_messages_invalid_since_returns_400():
    """A malformed 'since' value returns 400."""
    _install_token()
    try:
        with patch("app.api.routes.messages.get_client"):
            response = client.get(
                "/api/messages",
                params={"channel": "Public", "since": "not-a-date"},
                headers={"x-api-token": _VALID_TOKEN},
            )
        assert response.status_code == 400
        assert "since" in response.json()["detail"]["message"].lower()
    finally:
        _remove_token()


# ── Happy path — offset pagination ───────────────────────────────────────────


def test_get_messages_offset_pagination_returns_messages():
    """GET /api/messages?channel=Public&from=0&limit=100 returns rows from ClickHouse."""
    _install_token()
    rows = [_make_row(f"msg {i}") for i in range(3)]
    ch_result = _make_ch_result(rows)

    with patch("app.api.routes.messages.get_client") as mock_get_client:
        mock_get_client.return_value.query.return_value = ch_result
        try:
            response = client.get(
                "/api/messages",
                params={"channel": "Public", "from": 0, "limit": 100},
                headers={"x-api-token": _VALID_TOKEN},
            )
        finally:
            _remove_token()

    assert response.status_code == 200
    body = response.json()
    assert body["channel"] == "Public"
    assert body["count"] == 3
    assert len(body["messages"]) == 3
    assert body["messages"][0]["text"] == "msg 0"
    assert body["messages"][0]["sender"] == "Alice"


def test_get_messages_default_limit_is_100():
    """Without explicit limit, the query is issued with limit=100."""
    _install_token()
    ch_result = _make_ch_result([])

    with patch("app.api.routes.messages.get_client") as mock_get_client:
        mock_get_client.return_value.query.return_value = ch_result
        try:
            response = client.get(
                "/api/messages",
                params={"channel": "Public"},
                headers={"x-api-token": _VALID_TOKEN},
            )
        finally:
            _remove_token()

    assert response.status_code == 200
    # Verify the query was called with limit=100 in the parameters.
    call_kwargs = mock_get_client.return_value.query.call_args
    params = call_kwargs.kwargs.get("parameters") or call_kwargs.args[1]
    assert params["limit"] == 100


def test_get_messages_offset_pagination_passes_offset_to_query():
    """The 'from' value is forwarded to ClickHouse as the OFFSET."""
    _install_token()
    ch_result = _make_ch_result([])

    with patch("app.api.routes.messages.get_client") as mock_get_client:
        mock_get_client.return_value.query.return_value = ch_result
        try:
            response = client.get(
                "/api/messages",
                params={"channel": "Public", "from": 50, "limit": 25},
                headers={"x-api-token": _VALID_TOKEN},
            )
        finally:
            _remove_token()

    assert response.status_code == 200
    call_kwargs = mock_get_client.return_value.query.call_args
    params = call_kwargs.kwargs.get("parameters") or call_kwargs.args[1]
    assert params["offset"] == 50
    assert params["limit"] == 25


def test_get_messages_empty_result_returns_empty_list():
    """A channel with no messages returns count=0 and an empty list."""
    _install_token()

    with patch("app.api.routes.messages.get_client") as mock_get_client:
        mock_get_client.return_value.query.return_value = _make_ch_result([])
        try:
            response = client.get(
                "/api/messages",
                params={"channel": "Public"},
                headers={"x-api-token": _VALID_TOKEN},
            )
        finally:
            _remove_token()

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 0
    assert body["messages"] == []


# ── Happy path — time-based (since) ──────────────────────────────────────────


def test_get_messages_since_returns_messages():
    """GET /api/messages?channel=Public&since=... returns rows from ClickHouse."""
    _install_token()
    rows = [_make_row("recent msg")]
    ch_result = _make_ch_result(rows)

    with patch("app.api.routes.messages.get_client") as mock_get_client:
        mock_get_client.return_value.query.return_value = ch_result
        try:
            response = client.get(
                "/api/messages",
                params={
                    "channel": "Public",
                    "since": "2026-02-10 18:59:07.541",
                },
                headers={"x-api-token": _VALID_TOKEN},
            )
        finally:
            _remove_token()

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["messages"][0]["text"] == "recent msg"


def test_get_messages_since_passes_datetime_to_query():
    """The parsed 'since' datetime is forwarded to ClickHouse as since_dt."""
    _install_token()
    ch_result = _make_ch_result([])

    with patch("app.api.routes.messages.get_client") as mock_get_client:
        mock_get_client.return_value.query.return_value = ch_result
        try:
            response = client.get(
                "/api/messages",
                params={"channel": "Public", "since": "2026-02-10T18:59:07.541"},
                headers={"x-api-token": _VALID_TOKEN},
            )
        finally:
            _remove_token()

    assert response.status_code == 200
    call_kwargs = mock_get_client.return_value.query.call_args
    params = call_kwargs.kwargs.get("parameters") or call_kwargs.args[1]
    assert isinstance(params["since_dt"], datetime)
    assert params["since_dt"] == datetime(2026, 2, 10, 18, 59, 7, 541000)


def test_get_messages_since_uses_no_limit_or_offset():
    """When 'since' is supplied the query must not contain offset/limit params."""
    _install_token()
    ch_result = _make_ch_result([])

    with patch("app.api.routes.messages.get_client") as mock_get_client:
        mock_get_client.return_value.query.return_value = ch_result
        try:
            client.get(
                "/api/messages",
                params={"channel": "Public", "since": "2026-02-10T18:59:07"},
                headers={"x-api-token": _VALID_TOKEN},
            )
        finally:
            _remove_token()

    call_kwargs = mock_get_client.return_value.query.call_args
    params = call_kwargs.kwargs.get("parameters") or call_kwargs.args[1]
    assert "limit" not in params
    assert "offset" not in params


# ── Error paths ───────────────────────────────────────────────────────────────


def test_get_messages_clickhouse_unavailable_returns_503():
    """A ClickHouse failure returns 503."""
    _install_token()

    with patch("app.api.routes.messages.get_client") as mock_get_client:
        mock_get_client.return_value.query.side_effect = Exception("connection refused")
        try:
            response = client.get(
                "/api/messages",
                params={"channel": "Public"},
                headers={"x-api-token": _VALID_TOKEN},
            )
        finally:
            _remove_token()

    assert response.status_code == 503
    assert response.json()["detail"]["status"] == "error"


# ── Order parameter ───────────────────────────────────────────────────────────


def test_get_messages_order_asc_is_default():
    """Without an explicit order param the SQL uses ASC."""
    _install_token()
    ch_result = _make_ch_result([])

    with patch("app.api.routes.messages.get_client") as mock_get_client:
        mock_get_client.return_value.query.return_value = ch_result
        try:
            client.get(
                "/api/messages",
                params={"channel": "Public"},
                headers={"x-api-token": _VALID_TOKEN},
            )
        finally:
            _remove_token()

    sql = mock_get_client.return_value.query.call_args.args[0]
    assert "ORDER BY received_at ASC" in sql


def test_get_messages_order_desc():
    """order=desc produces DESC in the SQL."""
    _install_token()
    ch_result = _make_ch_result([])

    with patch("app.api.routes.messages.get_client") as mock_get_client:
        mock_get_client.return_value.query.return_value = ch_result
        try:
            client.get(
                "/api/messages",
                params={"channel": "Public", "order": "desc"},
                headers={"x-api-token": _VALID_TOKEN},
            )
        finally:
            _remove_token()

    sql = mock_get_client.return_value.query.call_args.args[0]
    assert "ORDER BY received_at DESC" in sql


def test_get_messages_invalid_order_returns_422():
    """An order value other than 'asc' or 'desc' returns 422."""
    _install_token()
    try:
        response = client.get(
            "/api/messages",
            params={"channel": "Public", "order": "random"},
            headers={"x-api-token": _VALID_TOKEN},
        )
        assert response.status_code == 422
    finally:
        _remove_token()
