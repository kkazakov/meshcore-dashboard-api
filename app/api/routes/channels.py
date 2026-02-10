"""
GET /api/channels — list channels configured on the connected companion device.

Authentication
--------------
Requires a valid ``x-api-token`` header obtained from ``POST /api/login``.

The endpoint connects to the local MeshCore companion device, fetches each
channel by index (starting at 0) until the device returns an error or no
response, and returns the full list.

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
from meshcore import EventType

logger = logging.getLogger(__name__)

router = APIRouter()

# Maximum number of channel slots to probe.  MeshCore firmware caps at 8.
_MAX_CHANNEL_SLOTS = 8


class ChannelInfo(BaseModel):
    index: int
    name: str
    secret_hex: str


class ChannelsResponse(BaseModel):
    status: str
    channels: list[ChannelInfo]


@router.get("/api/channels", response_model=ChannelsResponse)
async def get_channels(
    _email: str = Depends(require_token),
) -> ChannelsResponse:
    """
    Return the list of channels configured on the connected MeshCore companion
    device.

    Iterates channel indices 0 – 7 and stops as soon as the device responds
    with an error (indicating no more channels are configured).

    - **401** — invalid or missing ``x-api-token``.
    - **502** — device connection failed.
    - **504** — device did not respond to the channel query.

    Example response:

    ```json
    {
      "status": "ok",
      "channels": [
        { "index": 0, "name": "General", "secret_hex": "0a1b2c..." },
        { "index": 1, "name": "Admin",   "secret_hex": "ff00aa..." }
      ]
    }
    ```
    """
    config = telemetry_common.load_config()
    meshcore = None

    try:
        try:
            meshcore = await telemetry_common.connect_to_device(config, verbose=False)
        except Exception as exc:
            logger.error("Failed to connect to MeshCore device: %s", exc)
            raise HTTPException(
                status_code=502,
                detail={
                    "status": "error",
                    "message": f"Device connection failed: {exc}",
                },
            ) from exc

        channels: list[dict[str, Any]] = []

        for idx in range(_MAX_CHANNEL_SLOTS):
            try:
                event = await meshcore.commands.get_channel(idx)
            except Exception as exc:
                logger.warning("Error fetching channel %d: %s", idx, exc)
                break

            if event is None or event.type == EventType.ERROR:
                # No more channels configured at this index
                break

            payload = event.payload
            secret_raw = payload.get("channel_secret", b"")
            # channel_secret may arrive as bytes or already hex-encoded str
            if isinstance(secret_raw, (bytes, bytearray)):
                secret_hex = secret_raw.hex()
            else:
                secret_hex = str(secret_raw)

            name = payload.get("channel_name", "")
            # Skip uninitialised slots: empty name + all-zero secret
            if not name and all(b == "0" for b in secret_hex):
                continue

            channels.append(
                {
                    "index": payload.get("channel_idx", idx),
                    "name": name,
                    "secret_hex": secret_hex,
                }
            )

        if not channels and _MAX_CHANNEL_SLOTS > 0:
            # Connected successfully but got nothing — treat as empty list, not
            # an error (the device may genuinely have no channels configured).
            logger.info("No channels found on the connected device")

        return ChannelsResponse(
            status="ok",
            channels=[ChannelInfo(**ch) for ch in channels],
        )

    finally:
        if meshcore:
            try:
                await asyncio.wait_for(meshcore.disconnect(), timeout=5)
            except Exception:
                pass
