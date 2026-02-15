"""
WebSocket endpoint for real-time message broadcasting.

WebSocket /ws
-------------
Authenticated WebSocket endpoint that receives real-time notifications
when new messages are stored to ClickHouse by the poller.

Authentication
--------------
Clients must send an authentication message immediately after connection:
    {"type": "auth", "token": "<api-token>"}

On success, the server responds:
    {"type": "welcome", "email": "user@example.com"}

On failure, the server closes the connection.

Message format
--------------
New messages are broadcast as:
    {"type": "new_message", "data": {...}}
Where data contains: received_at, channel_name, sender_name, text, msg_type, snr

Usage
-----
    import websockets

    async with websockets.connect("ws://localhost:8000/ws") as ws:
        await ws.send_json({"type": "auth", "token": "abc123..."})
        response = await ws.recv_json()

        while True:
            message = await ws.recv_json()
            if message["type"] == "new_message":
                print(f"New message: {message['data']['text']}")
"""

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from app.api.deps import require_token
from app.events import add_client, get_message_queue, remove_client

logger = logging.getLogger(__name__)
router = APIRouter()


class AuthMessage(BaseModel):
    """Authentication message from client."""

    type: str
    token: str


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """
    WebSocket endpoint for real-time message broadcasting.

    Clients must authenticate by sending {"type": "auth", "token": "<token>"}.
    On success, they receive {"type": "welcome", "email": "..."}.
    New messages are then broadcast as {"type": "new_message", "data": {...}}.
    """
    await websocket.accept()

    conn: Any | None = None

    try:
        auth_msg = await websocket.receive_json()

        if auth_msg.get("type") != "auth":
            logger.warning("First message must be auth type")
            await websocket.close(
                code=4003, reason="First message must be authentication"
            )
            return

        token = auth_msg.get("token")
        if not token:
            logger.warning("Auth message missing token")
            await websocket.close(code=4003, reason="Missing token")
            return

        email = require_token(x_api_token=token)

        conn = await add_client(websocket, email)

        await websocket.send_json({"type": "welcome", "email": email})

        queue = get_message_queue()

        while True:
            try:
                msg = await asyncio.wait_for(queue.get(), timeout=30.0)
                await websocket.send_json({"type": "new_message", "data": msg})
            except asyncio.TimeoutError:
                continue

    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected")
    except Exception as exc:
        logger.error("WebSocket error: %s", exc)
    finally:
        if conn is not None:
            await remove_client(conn)
