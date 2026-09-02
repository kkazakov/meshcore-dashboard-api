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
- ``region``       : optional MeshCore region/scope the channel is scoped to

Channel Types
-------------
**Public Channels**: Use a ``#`` prefix (e.g., ``#public``). The secret is
auto-generated from the channel name using SHA-256, meaning anyone who creates
a channel with the same name will get the same secret.

**Private Channels**: Provide a custom ``password`` in the request. The secret
is derived from the password using SHA-256, ensuring only those who know the
password can communicate on the channel.

Regions
-------
``POST /api/channels`` accepts an optional ``region`` field. When provided, the
region is passed to the meshcore companion client via ``set_flood_scope`` and
stored in the ``channel_config`` table so ``GET /api/channels`` returns it.
Region names follow the MeshCore rules: lowercase alphanumeric plus hyphens,
at most 29 bytes.

Soft Delete
-----------
The DELETE endpoint supports soft delete via the ``soft`` parameter. When set
to ``true``, the channel is marked as deleted in the ``channel_config`` table
without clearing the slot on the device. This allows for quick restoration by
re-creating the channel with the same name.
"""

import asyncio
import logging
import re
from hashlib import sha256
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.deps import require_token
from app.db.clickhouse import get_client
from app.meshcore import telemetry_common
from app.meshcore.channel_cache import (
    get_cached_channels,
    invalidate_cache,
    load_channel_regions,
    populate_cache,
    set_cache,
)
from app.meshcore.connection import device_lock
from meshcore import EventType

logger = logging.getLogger(__name__)

router = APIRouter()

# Maximum number of channel slots to probe.  MeshCore firmware caps at 8.
_MAX_CHANNEL_SLOTS = 8

# MeshCore region/scope rules (see region filtering docs): max 29 UTF-8 bytes,
# lowercase alphanumeric plus hyphens only.
_REGION_MAX_BYTES = 29
_REGION_PATTERN = re.compile(r"^[a-z0-9-]+$")


# ── Pydantic models ───────────────────────────────────────────────────────────


class ChannelInfo(BaseModel):
    index: int
    name: str
    secret_hex: str
    region: str | None = None


class ChannelsResponse(BaseModel):
    status: str
    channels: list[ChannelInfo]


class CreateChannelRequest(BaseModel):
    name: str
    password: str | None = None
    region: str | None = None


class DeleteChannelRequest(BaseModel):
    name: str
    soft: bool = False


# ── Internal helpers ──────────────────────────────────────────────────────────


def _is_empty_slot(name: str, secret_hex: str) -> bool:
    """Return True for uninitialised device slots (blank name + zero secret)."""
    return not name and all(c == "0" for c in secret_hex)


def _filter_soft_deleted_channels(
    channels: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Filter out channels that are marked as deleted in channel_config.

    Queries the channel_config table for deleted channels and excludes them
    from the response.
    """
    if not channels:
        return channels

    try:
        client = get_client()
        channel_names = [ch["name"] for ch in channels]

        # Query for all deleted channels (use FINAL for immediate consistency)
        result = client.query(
            "SELECT channel_name FROM channel_config FINAL WHERE deleted = true"
        )

        deleted_names = set(row[0] for row in result.result_rows)
        return [ch for ch in channels if ch["name"] not in deleted_names]
    except Exception as exc:
        logger.error("Error filtering soft-deleted channels: %s", exc)
        return channels


async def _fetch_all_channels(meshcore: Any) -> list[dict[str, Any]]:
    """
    Iterate all channel slots on the device and return initialised ones.

    Reads up to ``_MAX_CHANNEL_SLOTS`` indices; empty/uninitialised slots are
    skipped.  Stops early if the device returns ERROR (no more slots).  Each
    channel is enriched with its region from ``channel_config``.
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

    regions = load_channel_regions()
    for ch in channels:
        ch["region"] = regions.get(ch["name"])

    return channels


def _normalize_region(region: str | None) -> str | None:
    """
    Validate and normalise an optional channel region/scope.

    Returns ``None`` for absent or blank input.  A leading ``#`` is stripped
    (the companion client re-adds it internally).  Raises ``HTTPException(400)``
    for names that violate the MeshCore region rules (max 29 UTF-8 bytes,
    lowercase alphanumeric and hyphens only).
    """
    if region is None:
        return None

    region = region.strip().lstrip("#")
    if not region:
        return None

    if len(region.encode("utf-8")) > _REGION_MAX_BYTES:
        raise HTTPException(
            status_code=400,
            detail={
                "status": "error",
                "message": (
                    f"Region must be at most {_REGION_MAX_BYTES} bytes"
                ),
            },
        )
    if not _REGION_PATTERN.fullmatch(region):
        raise HTTPException(
            status_code=400,
            detail={
                "status": "error",
                "message": (
                    "Region must contain only lowercase letters, digits and "
                    "hyphens"
                ),
            },
        )
    return region


async def _set_flood_scope(meshcore: Any, channel_name: str, region: str) -> None:
    """
    Provide the region/scope to the meshcore companion client.

    Best-effort: a failed ``set_flood_scope`` is logged as an error but does
    not fail the request — the channel itself was already written to the
    device, and the region is still persisted for ``GET /api/channels``.
    """
    try:
        result = await meshcore.commands.set_flood_scope(region)
    except Exception as exc:
        logger.error(
            "set_flood_scope failed for channel '%s' region '%s': %s",
            channel_name,
            region or "*",
            exc,
        )
        return
    if result is None or result.type == EventType.ERROR:
        err_msg = result.payload if result else "no response"
        logger.error(
            "Device rejected flood scope for channel '%s' region '%s': %s",
            channel_name,
            region or "*",
            err_msg,
        )


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

    Soft-deleted channels (marked in ``channel_config`` table) are excluded
    from the response.

    - **401** — invalid or missing ``x-api-token``.
    - **502** — device connection failed (cache cold and device unreachable).
    """
    cached = get_cached_channels()
    if cached is not None:
        logger.debug("GET /api/channels — cache hit (%d channels)", len(cached))
        filtered = _filter_soft_deleted_channels(cached)
        return ChannelsResponse(
            status="ok",
            channels=[ChannelInfo(**ch) for ch in filtered],
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

    filtered = _filter_soft_deleted_channels(channels)
    return ChannelsResponse(
        status="ok",
        channels=[ChannelInfo(**ch) for ch in filtered],
    )


@router.post("/api/channels", response_model=ChannelsResponse, status_code=201)
async def create_channel(
    payload: CreateChannelRequest,
    _email: str = Depends(require_token),
) -> ChannelsResponse:
    """
    Create a new channel on the next free slot of the connected MeshCore
    companion device.

    **Name-based (Public) Channels**: Omit the ``password`` field. The secret
    is auto-generated from the channel name using SHA-256. Anyone creating a
    channel with the same name will get the same secret. Conventionally, use
    a ``#`` prefix (e.g., ``#public``) to indicate a public channel.

    **Private Channels**: Provide a ``password`` in the request. The secret is
    derived from the password using SHA-256, ensuring only those who know the
    password can access the channel.

    **Regions (Scope)**: Optionally provide a ``region`` to scope the channel
    to a MeshCore region.  The region is provided to the companion client via
    ``set_flood_scope`` and is stored in the ``channel_config`` table so it is
    returned by ``GET /api/channels``.  Region names must be lowercase
    alphanumeric plus hyphens, at most 29 bytes (a leading ``#`` is accepted
    and stripped).

    **Restoring Soft-Deleted Channels**: If a channel with the same name exists
    on the device and was previously soft-deleted (marked in ``channel_config``
    table), this endpoint will restore it by setting ``deleted=false`` without
    re-writing the slot.

    After a successful write the channel cache is invalidated and immediately
    refreshed so that the updated list is returned in the response.

    - **400** — no free slot available, empty name, or invalid region.
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

    channel_region = _normalize_region(payload.region)

    # Determine channel secret based on password and name
    channel_secret: bytes | None = None

    if payload.password:
        # Private channel with custom password
        channel_secret = sha256(payload.password.encode("utf-8")).digest()[0:16]
        logger.info(
            "Creating private channel '%s' with password-derived secret",
            channel_name,
        )
    else:
        # Public channel or name-based channel - secret auto-generated by MeshCore
        # MeshCore auto-generates when: name starts with # OR channel_secret is None
        logger.info(
            "Creating channel '%s' with auto-generated secret from name", channel_name
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
            existing_idx: int | None = None

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
                    existing_idx = idx
                    break

            # Check if channel exists in channel_config with deleted=true
            if existing_idx is not None:
                try:
                    client = get_client()
                    result = client.query(
                        "SELECT deleted, region FROM channel_config "
                        "WHERE channel_name = {name:String}",
                        parameters={"name": channel_name},
                    )
                    if result.result_rows and result.result_rows[0][0]:
                        # Channel was soft-deleted, restore it
                        existing_region = result.result_rows[0][1] or ""
                        restore_region = (
                            channel_region if channel_region is not None else existing_region
                        )
                        logger.info(
                            "Restoring soft-deleted channel '%s' at slot %d (region=%s)",
                            channel_name,
                            existing_idx,
                            restore_region or "*",
                        )
                        client.command(
                            """
                            INSERT INTO channel_config
                                (channel_name, region, deleted, updated_at)
                            VALUES ({name:String}, {region:String}, false, now64())
                            """,
                            parameters={
                                "name": channel_name,
                                "region": restore_region,
                            },
                        )
                        if restore_region:
                            await _set_flood_scope(meshcore, channel_name, restore_region)
                        invalidate_cache()
                        channels = await _fetch_all_channels(meshcore)
                        set_cache(channels)
                        logger.info(
                            "Channel cache refreshed after restore (%d channels)",
                            len(channels),
                        )
                        filtered = _filter_soft_deleted_channels(channels)
                        return ChannelsResponse(
                            status="ok",
                            channels=[ChannelInfo(**ch) for ch in filtered],
                        )
                except Exception as exc:
                    logger.error("Error checking channel_config: %s", exc)

            # Channel exists on device and wasn't soft-deleted
            if existing_idx is not None:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "status": "error",
                        "message": f"Channel '{channel_name}' already exists at index {existing_idx}",
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
                # Pass custom secret for private channels, None for public
                result = await meshcore.commands.set_channel(
                    free_slot, channel_name, channel_secret=channel_secret
                )
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

            # Provide the region/scope to the companion client and persist it
            # so GET /api/channels can report it.
            if channel_region:
                await _set_flood_scope(meshcore, channel_name, channel_region)
                try:
                    client = get_client()
                    client.command(
                        """
                        INSERT INTO channel_config
                            (channel_name, region, deleted, updated_at)
                        VALUES ({name:String}, {region:String}, false, now64())
                        """,
                        parameters={
                            "name": channel_name,
                            "region": channel_region,
                        },
                    )
                except Exception as exc:
                    logger.error(
                        "Failed to store region in channel_config for '%s': %s",
                        channel_name,
                        exc,
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

    filtered = _filter_soft_deleted_channels(channels)
    return ChannelsResponse(
        status="ok",
        channels=[ChannelInfo(**ch) for ch in filtered],
    )


@router.delete("/api/channels", response_model=ChannelsResponse)
async def delete_channel(
    payload: DeleteChannelRequest,
    _email: str = Depends(require_token),
) -> ChannelsResponse:
    """
    Delete a channel by name from the connected MeshCore companion device.

    **Hard Delete (default)**: Clears the slot by overwriting it with an empty
    name and a zero secret, which is how MeshCore marks a slot as uninitialised.

    **Soft Delete**: Set ``soft=true`` in the request to mark the channel as
    deleted in the ``channel_config`` table without touching the device. The
    channel slot remains intact and can be restored by re-creating a channel
    with the same name.

    After a successful delete the channel cache is invalidated and immediately
    refreshed so that the updated list is returned in the response.

    - **400** — request name is empty.
    - **404** — no channel with that name exists on the device.
    - **401** — invalid or missing ``x-api-token``.
    - **500** — soft delete failed (database error).
    - **502** — device connection failed or write was rejected.
    - **504** — device did not acknowledge the delete.
    """
    channel_name = payload.name.strip()
    if not channel_name:
        raise HTTPException(
            status_code=400,
            detail={"status": "error", "message": "Channel name must not be empty"},
        )

    # Soft delete: mark as deleted in channel_config without touching the device
    if payload.soft:
        try:
            client = get_client()
            client.command(
                """
                INSERT INTO channel_config (channel_name, deleted, updated_at)
                VALUES ({name:String}, true, now64())
                """,
                parameters={"name": channel_name},
            )
            logger.info("Soft-deleted channel '%s'", channel_name)
            invalidate_cache()
            channels = await populate_cache()
            set_cache(channels)
            logger.info(
                "Channel cache refreshed after soft delete (%d channels)",
                len(channels),
            )
            filtered = _filter_soft_deleted_channels(channels)
            return ChannelsResponse(
                status="ok",
                channels=[ChannelInfo(**ch) for ch in filtered],
            )
        except Exception as exc:
            logger.error("Failed to soft-delete channel: %s", exc)
            raise HTTPException(
                status_code=500,
                detail={
                    "status": "error",
                    "message": f"Failed to soft-delete channel: {exc}",
                },
            ) from exc

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

    filtered = _filter_soft_deleted_channels(channels)
    return ChannelsResponse(
        status="ok",
        channels=[ChannelInfo(**ch) for ch in filtered],
    )
