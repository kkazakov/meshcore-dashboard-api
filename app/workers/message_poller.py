"""
Message poller — background task that drains the MeshCore device message queue
every 2 seconds and persists new messages to the ClickHouse ``messages`` table.

Design
------
- The MeshCore companion device supports only one connection at a time.  The
  poller acquires the global ``device_lock`` on each cycle, connects, drains
  the queue, then disconnects — exactly like the API routes do.  This ensures
  the poller and API routes never compete for the device.
- On each poll cycle ``get_msg()`` is called in a loop until the device
  responds with ``NO_MORE_MSGS`` (or returns ``None``), draining the full queue.
- Channel names and contact names are resolved on every connection (cheap: only
  a few round-trips) so the caches stay fresh after channel/contact changes.
- ClickHouse inserts are executed in a thread pool so they don't block the
  async event loop.
- If the lock is already held by an API route, ``device_lock.acquire()`` will
  wait until the route is done — no polling cycle is skipped.
- On connection failure the poller backs off exponentially (2 s → 4 s … 60 s).
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from app.db.clickhouse import get_client
from app.events import queue_message
from app.meshcore import telemetry_common
from app.meshcore.connection import device_lock
from meshcore import EventType

logger = logging.getLogger(__name__)

# How often (seconds) to poll for new messages during normal operation
POLL_INTERVAL: float = 2.0

# Back-off config for connection failures
_BACKOFF_BASE: float = 2.0
_BACKOFF_MAX: float = 60.0

# Maximum messages drained per cycle (safety cap)
_MAX_DRAIN: int = 200

# Timeout (seconds) for a single get_msg() call
_MSG_TIMEOUT: float = 3.0


# ── ClickHouse insert ─────────────────────────────────────────────────────────


def _insert_messages(rows: list[dict[str, Any]]) -> None:
    """Insert a batch of message rows into ClickHouse (runs in a thread)."""
    if not rows:
        return

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
        for row in rows
    ]
    client.insert("messages", data, column_names=column_names)
    logger.info("Inserted %d message(s) into ClickHouse", len(rows))


async def _queue_messages_for_broadcast(rows: list[dict[str, Any]]) -> None:
    """Queue messages for WebSocket broadcast."""
    if not rows:
        return

    for row in rows:
        success = await queue_message(row)
        if not success:
            logger.warning("Failed to queue message for broadcast (queue full)")


# ── Name resolution helpers ───────────────────────────────────────────────────


async def _build_channel_cache(meshcore: Any) -> dict[int, str]:
    """Return {channel_idx: channel_name} for all initialised slots."""
    cache: dict[int, str] = {}
    for idx in range(8):
        try:
            event = await meshcore.commands.get_channel(idx)
        except Exception as exc:
            logger.warning("Error reading channel %d: %s", idx, exc)
            break
        if event is None or event.type == EventType.ERROR:
            break
        payload = event.payload
        name = payload.get("channel_name", "")
        secret_raw = payload.get("channel_secret", b"")
        secret_hex = (
            secret_raw.hex()
            if isinstance(secret_raw, (bytes, bytearray))
            else str(secret_raw)
        )
        if not name and all(c == "0" for c in secret_hex):
            continue
        cache[payload.get("channel_idx", idx)] = name
    return cache


async def _build_contact_cache(meshcore: Any) -> dict[str, str]:
    """Return {pubkey_prefix_12hex: adv_name} for all contacts."""
    cache: dict[str, str] = {}
    try:
        result = await meshcore.commands.get_contacts()
    except Exception as exc:
        logger.warning("Failed to fetch contacts: %s", exc)
        return cache
    if result is None or result.type == EventType.ERROR:
        return cache
    for contact in (result.payload or {}).values():
        pk: str = contact.get("public_key", "") or ""
        name: str = contact.get("adv_name", "") or ""
        if pk and name:
            cache[pk[:12].lower()] = name
    return cache


# ── Text parsing ──────────────────────────────────────────────────────────────


def _split_sender_and_text(raw: str) -> tuple[str, str]:
    """
    Channel messages arrive as ``"SenderName: body"``.
    Returns ``(sender_name, body)``.  Falls back to ``("", raw)`` if no
    separator is found.
    """
    if ": " in raw:
        sender, _, body = raw.partition(": ")
        return sender.strip(), body
    return "", raw


# ── Single poll cycle ─────────────────────────────────────────────────────────


async def _poll_once(config: dict[str, Any]) -> int:
    """
    Acquire the device lock, connect, drain the message queue, disconnect.
    Returns the number of messages stored.

    Waits for the lock if it is held by an API route or the repeater poller,
    ensuring only one caller accesses the device at a time.
    """
    meshcore = None
    async with device_lock:
        try:
            meshcore = await telemetry_common.connect_to_device(config, verbose=False)

            channel_cache = await _build_channel_cache(meshcore)
            contact_cache = await _build_contact_cache(meshcore)

            rows: list[dict[str, Any]] = []
            received_at = datetime.now(timezone.utc)

            for _ in range(_MAX_DRAIN):
                try:
                    event = await asyncio.wait_for(
                        meshcore.commands.get_msg(), timeout=_MSG_TIMEOUT
                    )
                except asyncio.TimeoutError:
                    logger.debug("get_msg() timed out — queue empty")
                    break
                except Exception as exc:
                    logger.warning("get_msg() raised: %s", exc)
                    break

                if event is None or event.type == EventType.NO_MORE_MSGS:
                    break

                if event.type == EventType.ERROR:
                    logger.warning("get_msg() ERROR: %s", event.payload)
                    break

                payload = event.payload

                if event.type == EventType.CHANNEL_MSG_RECV:
                    ch_idx = payload.get("channel_idx", -1)
                    sender_name, text = _split_sender_and_text(payload.get("text", ""))
                    rows.append(
                        {
                            "received_at": received_at,
                            "msg_type": "CHAN",
                            "channel_idx": ch_idx,
                            "channel_name": channel_cache.get(ch_idx, ""),
                            "sender_timestamp": payload.get("sender_timestamp", 0),
                            "sender_pubkey_prefix": "",
                            "sender_name": sender_name,
                            "path_len": payload.get("path_len", 0),
                            "snr": payload.get("SNR", 0.0),
                            "text": text,
                            "txt_type": payload.get("txt_type", 0),
                            "signature": "",
                        }
                    )

                elif event.type == EventType.CONTACT_MSG_RECV:
                    prefix = payload.get("pubkey_prefix", "").lower()
                    rows.append(
                        {
                            "received_at": received_at,
                            "msg_type": "PRIV",
                            "channel_idx": -1,
                            "channel_name": "",
                            "sender_timestamp": payload.get("sender_timestamp", 0),
                            "sender_pubkey_prefix": prefix,
                            "sender_name": contact_cache.get(prefix, ""),
                            "path_len": payload.get("path_len", 0),
                            "snr": payload.get("SNR", 0.0),
                            "text": payload.get("text", ""),
                            "txt_type": payload.get("txt_type", 0),
                            "signature": payload.get("signature", ""),
                        }
                    )

                else:
                    logger.debug("Unexpected event in queue: %s", event.type)

            if rows:
                await asyncio.to_thread(_insert_messages, rows)
                await _queue_messages_for_broadcast(rows)

            return len(rows)

        finally:
            if meshcore:
                try:
                    await asyncio.wait_for(meshcore.disconnect(), timeout=5)
                except Exception:
                    pass


# ── Main poller loop ──────────────────────────────────────────────────────────


async def run_message_poller() -> None:
    """
    Long-running coroutine: poll every ``POLL_INTERVAL`` seconds.

    Intended to be launched as a background task from the FastAPI lifespan and
    cancelled on shutdown.
    """
    logger.info("Message poller starting")
    config = telemetry_common.load_config()
    backoff: float = _BACKOFF_BASE

    while True:
        try:
            count = await _poll_once(config)
            if count > 0:
                logger.debug("Poll cycle: %d new message(s) stored", count)
            backoff = _BACKOFF_BASE  # reset on successful cycle
            await asyncio.sleep(POLL_INTERVAL)

        except asyncio.CancelledError:
            logger.info("Message poller cancelled — shutting down")
            break

        except Exception as exc:
            logger.error("Poll cycle failed: %s — retrying in %.0fs", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX)

    logger.info("Message poller stopped")
