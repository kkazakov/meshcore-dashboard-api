"""
GET    /api/channels — list channels configured on the connected companion device.
POST   /api/channels — create a new channel on the next free slot.
DELETE /api/channels — delete a channel by name (clears the slot on the device).

Authentication
--------------
All endpoints require a valid ``x-api-token`` header obtained from
``POST /api/login``.

Caching
-------
``GET /api/channels`` is served from an in-process cache (12-hour TTL) backed
by ``app.meshcore.channel_cache``.  The cache is populated on application start
and is immediately invalidated and refreshed after every successful ``POST`` or
``DELETE`` so callers always see a consistent state.

Each channel entry contains:
- ``index``        : channel slot index on the device
- ``name``         : human-readable channel name
- ``secret_hex``   : 16-byte channel secret encoded as a hex string
"""

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.deps import require_token
from app.meshcore import telemetry_common
from app.meshcore.channel_cache import (
    get_cached_channels,
    invalidate_cache,
    populate_cache,
    set_cache,
)
from app.meshcore.connection import device_lock
from meshcore import EventType

logger = logging.getLogger(__name__)

router = APIRouter()

# Maximum number of channel slots to probe.  MeshCore firmware caps at 8.
_MAX_CHANNEL_SLOTS = 8


# ── Pydantic models ───────────────────────────────────────────────────────────


class ChannelInfo(BaseModel):
    index: int
    name: str
    secret_hex: str


class ChannelsResponse(BaseModel):
    status: str
    channels: list[ChannelInfo]


class CreateChannelRequest(BaseModel):
    name: str
    password: str | None = None


class DeleteChannelRequest(BaseModel):
    name: str


# ── Internal helpers ──────────────────────────────────────────────────────────


def _is_empty_slot(name: str, secret_hex: str) -> bool:
    """Return True for uninitialised device slots (blank name + zero secret)."""
    return not name and all(c == "0" for c in secret_hex)


async def _fetch_all_channels(meshcore: Any) -> list[dict[str, Any]]:
    """
    Iterate all channel slots on the device and return initialised ones.

    Reads up to ``_MAX_CHANNEL_SLOTS`` indices; empty/uninitialised slots are
    skipped.  Stops early if the device returns ERROR (no more slots).
    """
    channels: list[dict[str, Any]] = []

    for idx in range(_MAX_CHANNEL_SLOTS):
        try:
            event = await meshcore.commands.get_channel(idx)
        except Exception as exc:
            logger.warning("Error fetching channel %d: %s", idx, exc)
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


# ── Routes ────────────────────────────────────────────────────────────────────


@router.get("/api/channels", response_model=ChannelsResponse)
async def get_channels(
    _email: str = Depends(require_token),
) -> ChannelsResponse:
    """
    Return the list of channels configured on the connected MeshCore companion
    device.

    Responses are served from an in-process cache (12-hour TTL).  The cache is
    populated on startup and refreshed automatically after every write
    (create / delete).  A device round-trip is only performed when the cache is
    cold or expired.

    - **401** — invalid or missing ``x-api-token``.
    - **502** — device connection failed (cache cold and device unreachable).
    """
    cached = get_cached_channels()
    if cached is not None:
        logger.debug("GET /api/channels — cache hit (%d channels)", len(cached))
        return ChannelsResponse(
            status="ok",
            channels=[ChannelInfo(**ch) for ch in cached],
        )

    logger.info("GET /api/channels — cache miss, fetching from device")
    try:
        channels = await populate_cache()
    except Exception as exc:
        logger.error("Failed to fetch channels from device: %s", exc)
        raise HTTPException(
            status_code=502,
            detail={
                "status": "error",
                "message": f"Device connection failed: {exc}",
            },
        ) from exc

    return ChannelsResponse(
        status="ok",
        channels=[ChannelInfo(**ch) for ch in channels],
    )


@router.post("/api/channels", response_model=ChannelsResponse, status_code=201)
async def create_channel(
    payload: CreateChannelRequest,
    _email: str = Depends(require_token),
) -> ChannelsResponse:
    """
    Create a new channel on the next free slot of the connected MeshCore
    companion device.

    The channel secret is derived automatically from the name (SHA-256 of the
    name, first 16 bytes — the same algorithm used by MeshCore firmware).

    After a successful write the channel cache is invalidated and immediately
    refreshed so that the updated list is returned in the response.

    - **400** — no free slot available (all 8 slots are occupied).
    - **409** — a channel with the same name already exists.
    - **401** — invalid or missing ``x-api-token``.
    - **502** — device connection failed.
    - **504** — device did not acknowledge the write.
    """
    channel_name = payload.name.strip()
    if not channel_name:
        raise HTTPException(
            status_code=400,
            detail={"status": "error", "message": "Channel name must not be empty"},
        )

    config = telemetry_common.load_config()
    meshcore = None

    async with device_lock:
        try:
            try:
                meshcore = await telemetry_common.connect_to_device(
                    config, verbose=False
                )
            except Exception as exc:
                logger.error("Failed to connect to MeshCore device: %s", exc)
                raise HTTPException(
                    status_code=502,
                    detail={
                        "status": "error",
                        "message": f"Device connection failed: {exc}",
                    },
                ) from exc

            # Read all slots to find duplicates and the first free slot.
            free_slot: int | None = None

            for idx in range(_MAX_CHANNEL_SLOTS):
                try:
                    event = await meshcore.commands.get_channel(idx)
                except Exception as exc:
                    logger.warning("Error fetching channel %d: %s", idx, exc)
                    break

                if event is None or event.type == EventType.ERROR:
                    break

                slot_payload = event.payload
                secret_raw = slot_payload.get("channel_secret", b"")
                secret_hex = (
                    secret_raw.hex()
                    if isinstance(secret_raw, (bytes, bytearray))
                    else str(secret_raw)
                )
                name = slot_payload.get("channel_name", "")

                if _is_empty_slot(name, secret_hex):
                    if free_slot is None:
                        free_slot = idx
                    continue

                if name.lower() == channel_name.lower():
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "status": "error",
                            "message": f"Channel '{name}' already exists at index {idx}",
                        },
                    )

            if free_slot is None:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "status": "error",
                        "message": "No free channel slot available (all 8 slots are occupied)",
                    },
                )

            logger.info("Creating channel '%s' at slot %d", channel_name, free_slot)
            try:
                result = await meshcore.commands.set_channel(free_slot, channel_name)
            except Exception as exc:
                logger.error("set_channel failed: %s", exc)
                raise HTTPException(
                    status_code=502,
                    detail={
                        "status": "error",
                        "message": f"Failed to write channel: {exc}",
                    },
                ) from exc

            if result is None or result.type == EventType.ERROR:
                err_msg = result.payload if result else "no response"
                raise HTTPException(
                    status_code=504,
                    detail={
                        "status": "error",
                        "message": f"Device did not acknowledge channel creation: {err_msg}",
                    },
                )

            channels = await _fetch_all_channels(meshcore)

        finally:
            if meshcore:
                try:
                    await asyncio.wait_for(meshcore.disconnect(), timeout=5)
                except Exception:
                    pass

    # Invalidate the stale cache and store the freshly read list so the next
    # GET is served instantly without another device round-trip.
    invalidate_cache()
    set_cache(channels)
    logger.info("Channel cache refreshed after create (%d channels)", len(channels))

    return ChannelsResponse(
        status="ok",
        channels=[ChannelInfo(**ch) for ch in channels],
    )


@router.delete("/api/channels", response_model=ChannelsResponse)
async def delete_channel(
    payload: DeleteChannelRequest,
    _email: str = Depends(require_token),
) -> ChannelsResponse:
    """
    Delete a channel by name from the connected MeshCore companion device.

    The slot is cleared by overwriting it with an empty name and a zero secret,
    which is how MeshCore marks a slot as uninitialised.

    After a successful delete the channel cache is invalidated and immediately
    refreshed so that the updated list is returned in the response.

    - **400** — request name is empty.
    - **404** — no channel with that name exists on the device.
    - **401** — invalid or missing ``x-api-token``.
    - **502** — device connection failed or write was rejected.
    - **504** — device did not acknowledge the delete.
    """
    channel_name = payload.name.strip()
    if not channel_name:
        raise HTTPException(
            status_code=400,
            detail={"status": "error", "message": "Channel name must not be empty"},
        )

    config = telemetry_common.load_config()
    meshcore = None

    async with device_lock:
        try:
            try:
                meshcore = await telemetry_common.connect_to_device(
                    config, verbose=False
                )
            except Exception as exc:
                logger.error("Failed to connect to MeshCore device: %s", exc)
                raise HTTPException(
                    status_code=502,
                    detail={
                        "status": "error",
                        "message": f"Device connection failed: {exc}",
                    },
                ) from exc

            target_idx: int | None = None

            for idx in range(_MAX_CHANNEL_SLOTS):
                try:
                    event = await meshcore.commands.get_channel(idx)
                except Exception as exc:
                    logger.warning("Error fetching channel %d: %s", idx, exc)
                    break

                if event is None or event.type == EventType.ERROR:
                    break

                slot_payload = event.payload
                secret_raw = slot_payload.get("channel_secret", b"")
                secret_hex = (
                    secret_raw.hex()
                    if isinstance(secret_raw, (bytes, bytearray))
                    else str(secret_raw)
                )
                name = slot_payload.get("channel_name", "")

                if _is_empty_slot(name, secret_hex):
                    continue

                if name.lower() == channel_name.lower():
                    target_idx = idx
                    break

            if target_idx is None:
                raise HTTPException(
                    status_code=404,
                    detail={
                        "status": "error",
                        "message": f"Channel '{channel_name}' not found",
                    },
                )

            logger.info("Deleting channel '%s' at slot %d", channel_name, target_idx)
            try:
                result = await meshcore.commands.set_channel(
                    target_idx, "", channel_secret=b"\x00" * 16
                )
            except Exception as exc:
                logger.error("set_channel (clear) failed: %s", exc)
                raise HTTPException(
                    status_code=502,
                    detail={
                        "status": "error",
                        "message": f"Failed to clear channel slot: {exc}",
                    },
                ) from exc

            if result is None or result.type == EventType.ERROR:
                err_msg = result.payload if result else "no response"
                raise HTTPException(
                    status_code=504,
                    detail={
                        "status": "error",
                        "message": f"Device did not acknowledge channel deletion: {err_msg}",
                    },
                )

            channels = await _fetch_all_channels(meshcore)

        finally:
            if meshcore:
                try:
                    await asyncio.wait_for(meshcore.disconnect(), timeout=5)
                except Exception:
                    pass

    # Invalidate the stale cache and store the freshly read list so the next
    # GET is served instantly without another device round-trip.
    invalidate_cache()
    set_cache(channels)
    logger.info("Channel cache refreshed after delete (%d channels)", len(channels))

    return ChannelsResponse(
        status="ok",
        channels=[ChannelInfo(**ch) for ch in channels],
    )
