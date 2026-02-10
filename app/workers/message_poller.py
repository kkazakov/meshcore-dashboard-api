"""
Message poller — background task that drains the MeshCore device message queue
every 2 seconds and persists new messages to the ClickHouse ``messages`` table.

Design
------
- A single persistent connection to the companion device is maintained for the
  lifetime of the poller.  If the connection drops, the poller attempts to
  reconnect with exponential back-off (cap: 60 s) before resuming normal polls.
- On each tick ``get_msg()`` is called in a loop until the device responds with
  ``NO_MORE_MSGS`` (or returns ``None``), draining the full queue.
- Channel names are resolved once per connection and cached in-memory.
- Sender names are resolved from the contacts list (also cached, refreshed on
  every successful connection).
- ClickHouse inserts are executed in a thread pool so they don't block the
  async event loop.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from app.db.clickhouse import get_client
from app.meshcore import telemetry_common
from meshcore import EventType

logger = logging.getLogger(__name__)

# How often (seconds) to poll for new messages during normal operation
POLL_INTERVAL: float = 2.0

# Back-off config for reconnection attempts
_BACKOFF_BASE: float = 2.0
_BACKOFF_MAX: float = 60.0

# Maximum number of messages drained in a single poll cycle (safety cap)
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


# ── Name resolution helpers ───────────────────────────────────────────────────


async def _build_channel_cache(meshcore: Any) -> dict[int, str]:
    """
    Fetch all channel slots and return a mapping of {channel_idx: channel_name}.
    Empty/uninitialised slots are excluded.
    """
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
        # Skip uninitialised slots
        if not name and all(c == "0" for c in secret_hex):
            continue
        cache[payload.get("channel_idx", idx)] = name
    logger.debug("Channel cache: %s", cache)
    return cache


async def _build_contact_cache(meshcore: Any) -> dict[str, str]:
    """
    Fetch all contacts and return a mapping of {pubkey_prefix_6: adv_name}.
    The prefix is the first 12 hex characters (6 bytes) of the public key.
    """
    cache: dict[str, str] = {}
    try:
        result = await meshcore.commands.get_contacts()
    except Exception as exc:
        logger.warning("Failed to fetch contacts for name resolution: %s", exc)
        return cache

    if result is None or result.type == EventType.ERROR:
        return cache

    contacts = result.payload or {}
    for contact in contacts.values():
        pk: str = contact.get("public_key", "") or ""
        name: str = contact.get("adv_name", "") or ""
        if pk and name:
            # Store by first 12 hex chars (= 6 bytes, matching pubkey_prefix)
            cache[pk[:12].lower()] = name
    logger.debug("Contact cache built with %d entries", len(cache))
    return cache


# ── Text parsing helpers ──────────────────────────────────────────────────────


def _split_sender_and_text(raw: str) -> tuple[str, str]:
    """
    Channel message text arrives as ``"SenderName: message body"``.
    Split on the first ``: `` and return ``(sender_name, text)``.
    If the separator is absent, return ``("", raw)`` so nothing is lost.
    """
    if ": " in raw:
        sender, _, body = raw.partition(": ")
        return sender.strip(), body
    return "", raw


# ── Core drain loop ───────────────────────────────────────────────────────────


async def _drain_queue(
    meshcore: Any,
    channel_cache: dict[int, str],
    contact_cache: dict[str, str],
) -> int:
    """
    Drain all pending messages from the device queue.

    Returns the number of messages inserted.
    """
    rows: list[dict[str, Any]] = []
    received_at = datetime.now(timezone.utc)

    for _ in range(_MAX_DRAIN):
        try:
            event = await asyncio.wait_for(
                meshcore.commands.get_msg(), timeout=_MSG_TIMEOUT
            )
        except asyncio.TimeoutError:
            logger.debug("get_msg() timed out — queue likely empty")
            break
        except Exception as exc:
            logger.warning("get_msg() raised: %s", exc)
            break

        if event is None or event.type == EventType.NO_MORE_MSGS:
            break

        if event.type == EventType.ERROR:
            logger.warning("get_msg() returned ERROR: %s", event.payload)
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
            # Unexpected event type in the message queue — log and continue
            logger.debug("Unexpected event in queue: %s", event.type)
            continue

    if rows:
        await asyncio.to_thread(_insert_messages, rows)

    return len(rows)


# ── Main poller loop ──────────────────────────────────────────────────────────


async def run_message_poller() -> None:
    """
    Long-running coroutine: connect → cache → poll loop → reconnect on error.

    Intended to be launched as a background task from the FastAPI lifespan and
    cancelled on shutdown.
    """
    logger.info("Message poller starting")
    backoff: float = _BACKOFF_BASE
    config = telemetry_common.load_config()

    while True:
        meshcore = None
        try:
            logger.info("Connecting to MeshCore device for message polling…")
            meshcore = await telemetry_common.connect_to_device(config, verbose=False)
            logger.info("Message poller connected")

            # Reset back-off on successful connection
            backoff = _BACKOFF_BASE

            # Build name-resolution caches once per connection
            channel_cache = await _build_channel_cache(meshcore)
            contact_cache = await _build_contact_cache(meshcore)

            # Notify the device we are ready to receive messages
            await meshcore.commands.send_appstart()

            # Normal poll loop
            while True:
                count = await _drain_queue(meshcore, channel_cache, contact_cache)
                if count:
                    logger.debug("Poll cycle: %d new message(s) stored", count)
                await asyncio.sleep(POLL_INTERVAL)

        except asyncio.CancelledError:
            logger.info("Message poller cancelled — shutting down")
            break

        except Exception as exc:
            logger.error(
                "Message poller error: %s — reconnecting in %.0fs", exc, backoff
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX)

        finally:
            if meshcore:
                try:
                    await asyncio.wait_for(meshcore.disconnect(), timeout=5)
                except Exception:
                    pass

    logger.info("Message poller stopped")
