"""
Messages endpoints.

POST /api/messages
------------------
Send a text message to a named channel on the connected MeshCore companion device.

Authentication
--------------
Requires a valid ``x-api-token`` header obtained from ``POST /api/login``.

Request body
------------
``channel`` : channel name, optionally prefixed with ``#`` (e.g. ``#test``
              or ``test``).  Matching is case-insensitive.
``message``  : UTF-8 text to send.

Responses
---------
- **200** — message queued for transmission; returns the resolved channel index
  and name.
- **400** — empty channel name or empty message.
- **401** — invalid or missing ``x-api-token``.
- **404** — no channel with the given name exists on the device.
- **502** — device connection failed or send was rejected.
- **504** — device did not acknowledge the send within the timeout.

GET /api/messages
-----------------
Fetch stored messages from ClickHouse for a given channel.

Query parameters
----------------
``channel``  : (required) channel name to filter on.
``from``     : (optional, int, default 0) offset for pagination; returns up to
               ``limit`` messages starting at this offset, ordered by
               ``received_at`` ascending.
``limit``    : (optional, int, default 100, max 1000) number of messages to
               return when using offset-based pagination.
``since``    : (optional, ISO-8601 datetime) return all messages received at or
               after this timestamp up to now. Mutually exclusive with ``from``.

Responses
---------
- **200** — list of matching messages.
- **400** — ``channel`` is empty, both ``from`` and ``since`` supplied, or
            ``since`` cannot be parsed.
- **401** — invalid or missing ``x-api-token``.
- **503** — ClickHouse unavailable.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.api.deps import require_token
from app.db.clickhouse import get_client
from app.events import queue_message
from app.meshcore import telemetry_common
from app.meshcore.connection import device_lock
from meshcore import EventType

logger = logging.getLogger(__name__)

router = APIRouter()

# Maximum number of channel slots to probe (matches firmware cap).
_MAX_CHANNEL_SLOTS = 8


# ── Pydantic models ───────────────────────────────────────────────────────────


class SendMessageRequest(BaseModel):
    channel: str
    message: str


class SendMessageResponse(BaseModel):
    status: str
    channel_index: int
    channel_name: str


class MessageRecord(BaseModel):
    ts: datetime
    sender: str
    hops: int
    text: str


class GetMessagesResponse(BaseModel):
    channel: str
    count: int
    messages: list[MessageRecord]


# ── Internal helpers ──────────────────────────────────────────────────────────


def _strip_hash(channel: str) -> str:
    """Remove a leading ``#`` from a channel name, if present."""
    return channel.lstrip("#")


async def _get_device_name(meshcore) -> str:
    """Fetch the device's own name via send_appstart()."""
    try:
        result = await meshcore.commands.send_appstart()
        if result and result.type != EventType.ERROR:
            return result.payload.get("name", "")
    except Exception as exc:
        logger.warning("Failed to get device name: %s", exc)
    return ""


def _insert_sent_message(row: dict[str, Any]) -> None:
    """Insert an outgoing message into ClickHouse (runs in a thread)."""
    client = get_client()
    column_names = [
        "received_at",
        "msg_type",
        "channel_idx",
        "channel_name",
        "sender_timestamp",
        "sender_pubkey_prefix",
        "sender_name",
        "path_len",
        "snr",
        "text",
        "txt_type",
        "signature",
    ]
    data = [
        [
            row["received_at"],
            row["msg_type"],
            row["channel_idx"],
            row["channel_name"],
            row["sender_timestamp"],
            row["sender_pubkey_prefix"],
            row["sender_name"],
            row["path_len"],
            row["snr"],
            row["text"],
            row["txt_type"],
            row["signature"],
        ]
    ]
    client.insert("messages", data, column_names=column_names)
    logger.debug("Inserted sent message into ClickHouse")


async def _resolve_channel_index(meshcore, channel_name: str) -> tuple[int, str]:
    """
    Scan device slots 0 – 7 and return ``(index, canonical_name)`` for the
    slot whose name matches *channel_name* (case-insensitive, ``#`` stripped).

    Raises ``HTTPException(404)`` if no matching channel is found.
    """
    needle = _strip_hash(channel_name).lower()

    for idx in range(_MAX_CHANNEL_SLOTS):
        try:
            event = await meshcore.commands.get_channel(idx)
        except Exception as exc:
            logger.warning("Error fetching channel %d: %s", idx, exc)
            break

        if event is None or event.type == EventType.ERROR:
            break

        payload = event.payload
        name: str = payload.get("channel_name", "")
        secret_raw = payload.get("channel_secret", b"")
        secret_hex = (
            secret_raw.hex()
            if isinstance(secret_raw, (bytes, bytearray))
            else str(secret_raw)
        )

        # Skip uninitialised slots.
        if not name and all(c == "0" for c in secret_hex):
            continue

        if _strip_hash(name).lower() == needle:
            return payload.get("channel_idx", idx), name

    raise HTTPException(
        status_code=404,
        detail={
            "status": "error",
            "message": f"Channel '{channel_name}' not found on device",
        },
    )


# ── Route ─────────────────────────────────────────────────────────────────────


@router.post("/api/messages", response_model=SendMessageResponse)
async def send_message(
    payload: SendMessageRequest,
    _email: str = Depends(require_token),
) -> SendMessageResponse:
    """
    Send a text message to a named channel on the connected MeshCore device.

    The ``channel`` field accepts names with or without a leading ``#``
    (e.g. ``"#test"`` and ``"test"`` are equivalent).  Matching against the
    channels configured on the device is case-insensitive.

    - **400** — ``channel`` or ``message`` is empty.
    - **401** — invalid or missing ``x-api-token``.
    - **404** — channel not found on the device.
    - **502** — device connection failed or the send command was rejected.
    - **504** — device did not acknowledge the send.
    """
    channel_name = payload.channel.strip()
    message_text = payload.message.strip()

    if not channel_name:
        raise HTTPException(
            status_code=400,
            detail={"status": "error", "message": "channel must not be empty"},
        )
    if not message_text:
        raise HTTPException(
            status_code=400,
            detail={"status": "error", "message": "message must not be empty"},
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

            chan_idx, chan_name = await _resolve_channel_index(meshcore, channel_name)
            device_name = await _get_device_name(meshcore)

            logger.info(
                "Sending message to channel '%s' (slot %d): %r",
                chan_name,
                chan_idx,
                message_text,
            )

            try:
                result = await asyncio.wait_for(
                    meshcore.commands.send_chan_msg(chan_idx, message_text),
                    timeout=10,
                )
            except asyncio.TimeoutError as exc:
                logger.error("send_chan_msg timed out for channel '%s'", chan_name)
                raise HTTPException(
                    status_code=504,
                    detail={
                        "status": "error",
                        "message": "Device did not acknowledge the message send (timeout)",
                    },
                ) from exc
            except Exception as exc:
                logger.error("send_chan_msg failed: %s", exc)
                raise HTTPException(
                    status_code=502,
                    detail={
                        "status": "error",
                        "message": f"Failed to send message: {exc}",
                    },
                ) from exc

            if result is None or result.type == EventType.ERROR:
                err_msg = result.payload if result else "no response"
                raise HTTPException(
                    status_code=502,
                    detail={
                        "status": "error",
                        "message": f"Device rejected the message: {err_msg}",
                    },
                )

            sent_row = {
                "received_at": datetime.now(timezone.utc),
                "msg_type": "CHAN",
                "channel_idx": chan_idx,
                "channel_name": chan_name,
                "sender_timestamp": int(datetime.now(timezone.utc).timestamp()),
                "sender_pubkey_prefix": "",
                "sender_name": device_name,
                "path_len": 0,
                "snr": 0.0,
                "text": message_text,
                "txt_type": 0,
                "signature": "",
            }
            try:
                await asyncio.to_thread(_insert_sent_message, sent_row)
            except Exception as exc:
                logger.warning("Failed to store sent message in ClickHouse: %s", exc)

            try:
                await queue_message(sent_row)
            except Exception as exc:
                logger.warning(
                    "Failed to queue sent message for WebSocket broadcast: %s", exc
                )

            return SendMessageResponse(
                status="ok",
                channel_index=chan_idx,
                channel_name=chan_name,
            )

        finally:
            if meshcore:
                try:
                    await asyncio.wait_for(meshcore.disconnect(), timeout=5)
                except Exception:
                    pass


# ── GET /api/messages ─────────────────────────────────────────────────────────

_COLUMNS = (
    "received_at",
    "sender_name",
    "path_len",
    "text",
)


@router.get("/api/messages", response_model=GetMessagesResponse)
def get_messages(
    channel: str = Query(..., description="Channel name to filter on"),
    from_offset: int = Query(
        default=0,
        alias="from",
        ge=0,
        description="Row offset for pagination (mutually exclusive with 'since')",
    ),
    limit: int = Query(
        default=100,
        ge=1,
        le=1000,
        description="Maximum number of messages to return (used with 'from')",
    ),
    since: str | None = Query(
        default=None,
        description="ISO-8601 datetime; return messages received at or after this timestamp",
    ),
    order: Literal["asc", "desc"] = Query(
        default="asc",
        description="Sort order for messages by received_at: 'asc' (oldest first) or 'desc' (newest first)",
    ),
    _email: str = Depends(require_token),
) -> GetMessagesResponse:
    """
    Fetch stored channel messages from ClickHouse.

    Two modes:

    * **Offset pagination** — supply ``from`` (and optionally ``limit``).
      Returns up to ``limit`` messages starting at the given row offset,
      ordered by ``received_at`` ascending.

    * **Time-based** — supply ``since`` (ISO-8601 datetime, e.g.
      ``2026-02-10 18:59:07.541``).  Returns all messages received from that
      timestamp up to now, ordered by ``received_at`` ascending.

    ``from`` and ``since`` are mutually exclusive; supplying both returns 400.
    """
    channel_name = channel.strip()
    if not channel_name:
        raise HTTPException(
            status_code=400,
            detail={"status": "error", "message": "channel must not be empty"},
        )

    # Validate mutual exclusivity: if the caller explicitly passed `from` > 0
    # alongside `since`, that is a conflict.  (from_offset == 0 is the default
    # so we only treat it as an explicit "from" when since is absent.)
    if since is not None and from_offset != 0:
        raise HTTPException(
            status_code=400,
            detail={
                "status": "error",
                "message": "'from' and 'since' are mutually exclusive",
            },
        )

    col_list = ", ".join(_COLUMNS)
    order_dir = order.upper()

    if since is not None:
        # Parse the timestamp supplied by the caller.
        try:
            since_dt = datetime.fromisoformat(since)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail={
                    "status": "error",
                    "message": f"Invalid 'since' timestamp: {exc}",
                },
            ) from exc

        sql = (
            f"SELECT {col_list} FROM messages FINAL "
            "WHERE channel_name = {channel_name:String} "
            "AND received_at >= {since_dt:DateTime64(3)} "
            f"ORDER BY received_at {order_dir}"
        )
        params: dict = {"channel_name": channel_name, "since_dt": since_dt}
    else:
        sql = (
            f"SELECT {col_list} FROM messages FINAL "
            "WHERE channel_name = {channel_name:String} "
            f"ORDER BY received_at {order_dir} "
            "LIMIT {limit:UInt32} OFFSET {offset:UInt32}"
        )
        params = {
            "channel_name": channel_name,
            "limit": limit,
            "offset": from_offset,
        }

    try:
        client = get_client()
        result = client.query(sql, parameters=params)
    except Exception as exc:
        logger.error("ClickHouse query failed in get_messages: %s", exc)
        raise HTTPException(
            status_code=503,
            detail={"status": "error", "message": "Database unavailable"},
        ) from exc

    records: list[MessageRecord] = []
    for received_at, sender_name, path_len, text in result.result_rows:
        records.append(
            MessageRecord(ts=received_at, sender=sender_name, hops=path_len, text=text)
        )

    return GetMessagesResponse(
        channel=channel_name,
        count=len(records),
        messages=records,
    )
