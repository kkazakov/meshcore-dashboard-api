"""
Message event bus for WebSocket broadcasting.

Handles queuing of new messages from the poller and broadcasting to all
authenticated WebSocket clients with debounced batching.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

_MAX_QUEUE_SIZE: int = 1000
_BROADCAST_DEBOUNCE: float = 1.0

_message_queue: asyncio.Queue | None = None
_connected_clients: set["WebSocketConnection"] = set()
_clients_lock: asyncio.Lock | None = None


@dataclass(frozen=True)
class WebSocketConnection:
    """Represents an authenticated WebSocket connection."""

    websocket: Any
    email: str
    authenticated_at: datetime


def get_message_queue() -> asyncio.Queue:
    """Return the global message queue, creating it if necessary."""
    global _message_queue
    if _message_queue is None:
        _message_queue = asyncio.Queue(maxsize=_MAX_QUEUE_SIZE)
    return _message_queue


def get_clients_lock() -> asyncio.Lock:
    """Return the global clients lock, creating it if necessary."""
    global _clients_lock
    if _clients_lock is None:
        _clients_lock = asyncio.Lock()
    return _clients_lock


async def add_client(websocket: Any, email: str) -> WebSocketConnection:
    """Add a connected client and return its connection record."""
    conn = WebSocketConnection(
        websocket=websocket,
        email=email,
        authenticated_at=datetime.now(),
    )
    async with get_clients_lock():
        _connected_clients.add(conn)
    logger.info("Client connected: %s (total: %d)", email, len(_connected_clients))
    return conn


async def remove_client(conn: WebSocketConnection) -> None:
    """Remove a disconnected client."""
    async with get_clients_lock():
        if conn in _connected_clients:
            _connected_clients.remove(conn)
            logger.info(
                "Client disconnected: %s (remaining: %d)",
                conn.email,
                len(_connected_clients),
            )


def get_connected_clients_count() -> int:
    """Return the number of connected authenticated clients."""
    return len(_connected_clients)


def transform_message(row: dict[str, Any]) -> dict[str, Any]:
    """
    Transform a ClickHouse message row into the WebSocket event format.
    """
    return {
        "received_at": (
            row["received_at"].isoformat()
            if isinstance(row["received_at"], datetime)
            else str(row["received_at"])
        ),
        "channel_name": row.get("channel_name", ""),
        "sender_name": row.get("sender_name", ""),
        "text": row.get("text", ""),
        "msg_type": row.get("msg_type", "CHAN"),
        "snr": float(row.get("snr", 0.0)) if row.get("snr") is not None else 0.0,
        "channel_idx": row.get("channel_idx", -1),
        "sender_timestamp": row.get("sender_timestamp", 0),
    }


async def queue_message(row: dict[str, Any]) -> bool:
    """Queue a transformed message for broadcast. Returns False if queue is full."""
    transformed = transform_message(row)
    try:
        queue = get_message_queue()
        await asyncio.wait_for(queue.put(transformed), timeout=0.1)
        return True
    except asyncio.QueueFull:
        logger.warning("Message queue full, dropping message")
        return False


async def broadcast_message(message: dict[str, Any]) -> None:
    """Broadcast a single message to all connected clients."""
    if not _connected_clients:
        return

    payload = {"type": "new_message", "data": message}
    disconnected: list[WebSocketConnection] = []

    async with get_clients_lock():
        clients = list(_connected_clients)

    for conn in clients:
        try:
            await conn.websocket.send_json(payload)
        except Exception as exc:
            logger.warning(
                "Failed to send to %s: %s - removing client", conn.email, exc
            )
            disconnected.append(conn)

    if disconnected:
        async with get_clients_lock():
            for conn in disconnected:
                _connected_clients.discard(conn)


async def broadcast_task() -> None:
    """
    Long-running task: collect messages from queue, debounce, then broadcast.
    """
    logger.info("Broadcast task starting")
    queue = get_message_queue()

    while True:
        try:
            batch: list[dict[str, Any]] = []
            try:
                first = await asyncio.wait_for(queue.get(), timeout=5.0)
                batch.append(first)
            except asyncio.TimeoutError:
                continue

            while len(batch) < 100:
                try:
                    msg = await asyncio.wait_for(
                        queue.get(), timeout=_BROADCAST_DEBOUNCE
                    )
                    batch.append(msg)
                except asyncio.TimeoutError:
                    break

            if batch:
                logger.debug("Broadcasting %d message(s)", len(batch))
                for msg in batch:
                    await broadcast_message(msg)

        except asyncio.CancelledError:
            logger.info("Broadcast task cancelled")
            break

        except Exception as exc:
            logger.error("Broadcast task error: %s", exc, exc_info=True)


async def stop_broadcast_task() -> None:
    """Cleanup on shutdown."""
    global _message_queue, _connected_clients, _clients_lock
    _connected_clients.clear()
    _message_queue = None
    _clients_lock = None
