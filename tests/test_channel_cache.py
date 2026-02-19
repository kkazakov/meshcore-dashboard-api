"""
Tests for the channel cache (app.meshcore.channel_cache) and the caching
behaviour of GET/POST/DELETE /api/channels.
"""

import json
import time
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import app.meshcore.channel_cache as cache_module
from app.main import app
from app.meshcore.channel_cache import (
    CACHE_TTL_SECONDS,
    get_cached_channels,
    invalidate_cache,
    populate_cache,
    set_cache,
)
from meshcore import EventType

client = TestClient(app)

_VALID_TOKEN = "test-token-channel-cache"

# ── Shared fixtures / helpers ─────────────────────────────────────────────────


def _mock_ch_client() -> MagicMock:
    """ClickHouse client that validates _VALID_TOKEN."""
    mock_result = MagicMock()
    mock_result.result_rows = [["test@example.com"]]
    mock_ch = MagicMock()
    mock_ch.query.return_value = mock_result
    return mock_ch


@contextmanager
def _valid_token():
    with patch("app.api.deps.get_client", return_value=_mock_ch_client()):
        yield _VALID_TOKEN


def _make_channel_event(idx: int, name: str, secret: bytes = b"\xab" * 16) -> MagicMock:
    evt = MagicMock()
    evt.type = EventType.OK
    evt.payload = {
        "channel_idx": idx,
        "channel_name": name,
        "channel_secret": secret,
    }
    return evt


def _make_empty_slot_event(idx: int) -> MagicMock:
    evt = MagicMock()
    evt.type = EventType.OK
    evt.payload = {
        "channel_idx": idx,
        "channel_name": "",
        "channel_secret": b"\x00" * 16,
    }
    return evt


def _make_error_event() -> MagicMock:
    evt = MagicMock()
    evt.type = EventType.ERROR
    evt.payload = "no more slots"
    return evt


def _make_ok_event() -> MagicMock:
    evt = MagicMock()
    evt.type = EventType.OK
    return evt


@pytest.fixture(autouse=True)
def reset_cache():
    """Ensure the cache is clean before and after every test."""
    invalidate_cache()
    yield
    invalidate_cache()


# ── Unit tests for cache_module functions ─────────────────────────────────────


class TestGetCachedChannels:
    def test_returns_none_when_empty(self):
        assert get_cached_channels() is None

    def test_returns_data_after_set_cache(self):
        data = [{"index": 0, "name": "alpha", "secret_hex": "ab" * 16}]
        set_cache(data)
        assert get_cached_channels() == data

    def test_returns_none_after_invalidate(self):
        set_cache([{"index": 0, "name": "alpha", "secret_hex": "ab" * 16}])
        invalidate_cache()
        assert get_cached_channels() is None

    def test_returns_none_when_expired(self, monkeypatch):
        data = [{"index": 0, "name": "alpha", "secret_hex": "ab" * 16}]
        set_cache(data)
        # Rewind the clock so the entry looks expired
        monkeypatch.setattr(
            cache_module,
            "_cache_populated_at",
            time.monotonic() - CACHE_TTL_SECONDS - 1,
        )
        assert get_cached_channels() is None

    def test_returns_data_just_before_expiry(self, monkeypatch):
        data = [{"index": 0, "name": "alpha", "secret_hex": "ab" * 16}]
        set_cache(data)
        # One second before expiry — should still be valid
        monkeypatch.setattr(
            cache_module,
            "_cache_populated_at",
            time.monotonic() - CACHE_TTL_SECONDS + 1,
        )
        assert get_cached_channels() == data


class TestSetCache:
    def test_stores_and_retrieves(self):
        channels = [{"index": 1, "name": "beta", "secret_hex": "cd" * 16}]
        set_cache(channels)
        assert get_cached_channels() == channels

    def test_overwrites_previous_entry(self):
        set_cache([{"index": 0, "name": "old", "secret_hex": "00" * 16}])
        new = [{"index": 0, "name": "new", "secret_hex": "ff" * 16}]
        set_cache(new)
        assert get_cached_channels() == new


class TestInvalidateCache:
    def test_clears_data(self):
        set_cache([{"index": 0, "name": "alpha", "secret_hex": "ab" * 16}])
        invalidate_cache()
        assert get_cached_channels() is None


@pytest.mark.asyncio
class TestPopulateCache:
    async def test_fetches_from_device_and_caches(self):
        mock_meshcore = AsyncMock()
        mock_meshcore.commands.get_channel = AsyncMock(
            side_effect=[
                _make_channel_event(0, "general"),
                _make_channel_event(1, "ops"),
                _make_error_event(),
            ]
        )
        mock_meshcore.disconnect = AsyncMock()

        with (
            patch(
                "app.meshcore.channel_cache.telemetry_common.connect_to_device",
                new=AsyncMock(return_value=mock_meshcore),
            ),
            patch(
                "app.meshcore.channel_cache.telemetry_common.load_config",
                return_value={},
            ),
        ):
            channels = await populate_cache()

        assert len(channels) == 2
        assert channels[0]["name"] == "general"
        assert channels[1]["name"] == "ops"
        # Cache should be warm now
        assert get_cached_channels() == channels

    async def test_skips_empty_slots(self):
        mock_meshcore = AsyncMock()
        mock_meshcore.commands.get_channel = AsyncMock(
            side_effect=[
                _make_channel_event(0, "general"),
                _make_empty_slot_event(1),
                _make_error_event(),
            ]
        )
        mock_meshcore.disconnect = AsyncMock()

        with (
            patch(
                "app.meshcore.channel_cache.telemetry_common.connect_to_device",
                new=AsyncMock(return_value=mock_meshcore),
            ),
            patch(
                "app.meshcore.channel_cache.telemetry_common.load_config",
                return_value={},
            ),
        ):
            channels = await populate_cache()

        assert len(channels) == 1
        assert channels[0]["name"] == "general"

    async def test_double_checked_locking_skips_fetch_if_warm(self):
        """Second caller after cache is already warm should not connect."""
        set_cache([{"index": 0, "name": "pre-warm", "secret_hex": "aa" * 16}])

        connect_mock = AsyncMock()
        with patch(
            "app.meshcore.channel_cache.telemetry_common.connect_to_device",
            new=connect_mock,
        ):
            result = await populate_cache()

        connect_mock.assert_not_awaited()
        assert result[0]["name"] == "pre-warm"


# ── Integration tests for GET /api/channels ────────────────────────────────────


class TestGetChannelsEndpoint:
    def test_cache_hit_does_not_call_device(self):
        """When cache is warm the device is never touched."""
        set_cache(
            [
                {"index": 0, "name": "general", "secret_hex": "ab" * 16},
                {"index": 1, "name": "ops", "secret_hex": "cd" * 16},
            ]
        )

        connect_mock = AsyncMock()
        with (
            _valid_token() as token,
            patch(
                "app.meshcore.channel_cache.telemetry_common.connect_to_device",
                new=connect_mock,
            ),
        ):
            response = client.get("/api/channels", headers={"x-api-token": token})

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert len(body["channels"]) == 2
        assert body["channels"][0]["name"] == "general"
        connect_mock.assert_not_awaited()

    def test_cache_miss_fetches_from_device(self):
        """Cold cache triggers a device connection."""
        mock_meshcore = AsyncMock()
        mock_meshcore.commands.get_channel = AsyncMock(
            side_effect=[
                _make_channel_event(0, "alpha"),
                _make_error_event(),
            ]
        )
        mock_meshcore.disconnect = AsyncMock()

        with (
            _valid_token() as token,
            patch(
                "app.meshcore.channel_cache.telemetry_common.connect_to_device",
                new=AsyncMock(return_value=mock_meshcore),
            ),
            patch(
                "app.meshcore.channel_cache.telemetry_common.load_config",
                return_value={},
            ),
        ):
            response = client.get("/api/channels", headers={"x-api-token": token})

        assert response.status_code == 200
        body = response.json()
        assert len(body["channels"]) == 1
        assert body["channels"][0]["name"] == "alpha"

    def test_device_error_returns_502(self):
        """Device connection failure on a cold cache returns 502."""
        with (
            _valid_token() as token,
            patch(
                "app.meshcore.channel_cache.telemetry_common.connect_to_device",
                new=AsyncMock(side_effect=RuntimeError("TCP timeout")),
            ),
            patch(
                "app.meshcore.channel_cache.telemetry_common.load_config",
                return_value={},
            ),
        ):
            response = client.get("/api/channels", headers={"x-api-token": token})

        assert response.status_code == 502

    def test_requires_auth(self):
        response = client.get("/api/channels")
        assert response.status_code == 401


# ── Integration tests for POST /api/channels cache invalidation ───────────────


class TestCreateChannelCacheInvalidation:
    def test_create_invalidates_and_refreshes_cache(self):
        """After POST the cache is updated with the new channel list."""
        # Pre-warm cache with stale data
        set_cache([{"index": 0, "name": "old-only", "secret_hex": "aa" * 16}])

        # Device will return two channels after the write
        mock_meshcore = AsyncMock()
        mock_meshcore.commands.get_channel = AsyncMock(
            side_effect=[
                # Slot scan: slot 0 occupied, slot 1 is free (empty), stops at ERROR
                _make_channel_event(0, "old-only"),
                _make_empty_slot_event(1),
                _make_error_event(),
                # _fetch_all_channels after write
                _make_channel_event(0, "old-only"),
                _make_channel_event(1, "new-channel"),
                _make_error_event(),
            ]
        )
        mock_meshcore.commands.set_channel = AsyncMock(return_value=_make_ok_event())
        mock_meshcore.disconnect = AsyncMock()

        with (
            _valid_token() as token,
            patch(
                "app.api.routes.channels.telemetry_common.connect_to_device",
                new=AsyncMock(return_value=mock_meshcore),
            ),
            patch(
                "app.api.routes.channels.telemetry_common.load_config",
                return_value={},
            ),
        ):
            response = client.post(
                "/api/channels",
                json={"name": "new-channel"},
                headers={"x-api-token": token},
            )

        assert response.status_code == 201
        body = response.json()
        assert any(ch["name"] == "new-channel" for ch in body["channels"])

        # Cache should now reflect updated list, not the stale pre-warm data
        cached = get_cached_channels()
        assert cached is not None
        assert any(ch["name"] == "new-channel" for ch in cached)


# ── Integration tests for DELETE /api/channels cache invalidation ─────────────


class TestDeleteChannelCacheInvalidation:
    def test_delete_invalidates_and_refreshes_cache(self):
        """After DELETE the cache is updated with the channel removed."""
        set_cache(
            [
                {"index": 0, "name": "keep", "secret_hex": "aa" * 16},
                {"index": 1, "name": "remove-me", "secret_hex": "bb" * 16},
            ]
        )

        mock_meshcore = AsyncMock()
        mock_meshcore.commands.get_channel = AsyncMock(
            side_effect=[
                # Slot scan to find target
                _make_channel_event(0, "keep"),
                _make_channel_event(1, "remove-me"),
                # _fetch_all_channels after write
                _make_channel_event(0, "keep"),
                _make_error_event(),
            ]
        )
        mock_meshcore.commands.set_channel = AsyncMock(return_value=_make_ok_event())
        mock_meshcore.disconnect = AsyncMock()

        with (
            _valid_token() as token,
            patch(
                "app.api.routes.channels.telemetry_common.connect_to_device",
                new=AsyncMock(return_value=mock_meshcore),
            ),
            patch(
                "app.api.routes.channels.telemetry_common.load_config",
                return_value={},
            ),
        ):
            response = client.request(
                "DELETE",
                "/api/channels",
                json={"name": "remove-me"},
                headers={"x-api-token": token},
            )

        assert response.status_code == 200
        body = response.json()
        names = [ch["name"] for ch in body["channels"]]
        assert "remove-me" not in names
        assert "keep" in names

        # Cache should reflect the deletion
        cached = get_cached_channels()
        assert cached is not None
        cached_names = [ch["name"] for ch in cached]
        assert "remove-me" not in cached_names
        assert "keep" in cached_names
