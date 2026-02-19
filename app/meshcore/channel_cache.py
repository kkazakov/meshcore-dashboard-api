"""
In-process cache for the MeshCore channels list.

The channels list is expensive to fetch (requires a device connection + up to
8 sequential slot reads), so it is cached for up to 12 hours.  The cache is
invalidated and immediately refreshed whenever a channel is created or deleted.

Public API
----------
- ``populate_cache()``  — connect to the device, fetch all channels, store.
- ``get_cached_channels()`` — return cached channels, or ``None`` if stale/empty.
- ``invalidate_cache()`` — clear the cache (triggers a fresh fetch on next read).
- ``CACHE_TTL_SECONDS``  — 12-hour TTL constant.

Thread / async safety
---------------------
All mutations are protected by a single ``asyncio.Lock`` so that concurrent
requests don't fan-out multiple device connections when the cache is cold.
"""

import asyncio
import logging
import time
from typing import Any

from app.meshcore import telemetry_common
from app.meshcore.connection import device_lock

logger = logging.getLogger(__name__)

# 12-hour TTL
CACHE_TTL_SECONDS: int = 12 * 60 * 60

# ── Cache state ───────────────────────────────────────────────────────────────

# Each entry is a plain dict matching ChannelInfo fields:
# {"index": int, "name": str, "secret_hex": str}
_cached_channels: list[dict[str, Any]] | None = None
_cache_populated_at: float = 0.0  # epoch seconds; 0 means never populated

# Protects writes to the two variables above.  Readers that find a warm cache
# skip the lock entirely for maximum throughput.
_cache_lock: asyncio.Lock = asyncio.Lock()


# ── Public helpers ────────────────────────────────────────────────────────────


def get_cached_channels() -> list[dict[str, Any]] | None:
    """
    Return the cached channels list if it is still within the 12-hour TTL.

    Returns ``None`` when the cache is empty or has expired.  Callers that
    receive ``None`` should call ``populate_cache()`` to refresh.
    """
    global _cached_channels, _cache_populated_at
    if _cached_channels is None:
        return None
    age = time.monotonic() - _cache_populated_at
    if age > CACHE_TTL_SECONDS:
        logger.info(
            "Channel cache expired (age=%.0fs, TTL=%ds)", age, CACHE_TTL_SECONDS
        )
        return None
    return _cached_channels


def invalidate_cache() -> None:
    """Immediately discard the cached channels list."""
    global _cached_channels, _cache_populated_at
    _cached_channels = None
    _cache_populated_at = 0.0
    logger.info("Channel cache invalidated")


def set_cache(channels: list[dict[str, Any]]) -> None:
    """
    Store a pre-fetched channel list in the cache and reset the TTL clock.

    Used by write operations (create / delete) that already hold an open device
    connection and have the fresh list available — avoids an extra round-trip
    just to warm the cache.
    """
    global _cached_channels, _cache_populated_at
    _cached_channels = channels
    _cache_populated_at = time.monotonic()
    logger.info(
        "Channel cache set externally: %d channel(s) cached for up to %dh",
        len(channels),
        CACHE_TTL_SECONDS // 3600,
    )


async def populate_cache() -> list[dict[str, Any]]:
    """
    Connect to the MeshCore device, fetch all channels, and store them in the
    cache.  Returns the freshly fetched channel list.

    If another coroutine is already refreshing the cache, this call waits for
    it to finish and then returns the result produced by the other coroutine
    (avoiding a duplicate device connection).

    Raises the same exceptions as ``connect_to_device`` / ``_fetch_all_channels``
    on failure; the cache is left in its previous state in that case.
    """
    global _cached_channels, _cache_populated_at

    async with _cache_lock:
        # Double-checked locking: another waiter may have already refreshed.
        if get_cached_channels() is not None:
            logger.debug(
                "Channel cache already warm after acquiring lock; skipping fetch"
            )
            return _cached_channels  # type: ignore[return-value]

        config = telemetry_common.load_config()
        meshcore = None

        async with device_lock:
            try:
                meshcore = await telemetry_common.connect_to_device(
                    config, verbose=False
                )
                channels = await _fetch_all_channels(meshcore)
            finally:
                if meshcore:
                    try:
                        await asyncio.wait_for(meshcore.disconnect(), timeout=5)
                    except Exception:
                        pass

        _cached_channels = channels
        _cache_populated_at = time.monotonic()
        logger.info(
            "Channel cache populated: %d channel(s) cached for up to %dh",
            len(channels),
            CACHE_TTL_SECONDS // 3600,
        )
        return channels


# ── Internal device helpers ───────────────────────────────────────────────────

_MAX_CHANNEL_SLOTS = 8


def _is_empty_slot(name: str, secret_hex: str) -> bool:
    return not name and all(c == "0" for c in secret_hex)


async def _fetch_all_channels(meshcore: Any) -> list[dict[str, Any]]:
    """Iterate 0-7 slots; skip empty; stop early on ERROR."""
    # Import here to avoid a circular dependency with channels.py which also
    # imports from this module.
    from meshcore import EventType  # noqa: PLC0415

    channels: list[dict[str, Any]] = []

    for idx in range(_MAX_CHANNEL_SLOTS):
        try:
            event = await meshcore.commands.get_channel(idx)
        except Exception as exc:
            logger.warning("Error fetching channel slot %d: %s", idx, exc)
            break

        if event is None or event.type == EventType.ERROR:
            break

        payload = event.payload
        secret_raw = payload.get("channel_secret", b"")
        secret_hex = (
            secret_raw.hex()
            if isinstance(secret_raw, (bytes, bytearray))
            else str(secret_raw)
        )
        name = payload.get("channel_name", "")

        if _is_empty_slot(name, secret_hex):
            continue

        channels.append(
            {
                "index": payload.get("channel_idx", idx),
                "name": name,
                "secret_hex": secret_hex,
            }
        )

    return channels
