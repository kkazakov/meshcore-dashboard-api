"""
Tests for WebSocket endpoint /ws.

Tests authentication and basic broadcast functionality.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from app.main import app


def test_websocket_auth_success():
    """WebSocket connection succeeds with valid token."""
    mock_email = "alice@example.com"
    
    async def run_test():
        from app.api.routes.websocket import router
        from fastapi import APIRouter
        
        ws_app = APIRouter()
        ws_app.include_router(router)
        
        client = TestClient(ws_app)
        
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({"type": "auth", "token": "fake_token"})
            response = websocket.receive_json()
            
            assert response["type"] == "welcome"
            assert response["email"] == mock_email
    
    with patch("app.api.routes.websocket.require_token", return_value=mock_email):
        with patch("app.api.routes.websocket.add_client", new_callable=AsyncMock) as mock_add:
            mock_add.return_value = MagicMock()
            with patch("app.api.routes.websocket.remove_client", new_callable=AsyncMock):
                with patch("app.api.routes.websocket.get_message_queue") as mock_queue:
                    queue = asyncio.Queue()
                    mock_queue.return_value = queue
                    asyncio.run(run_test())


def test_websocket_auth_failure_missing_token():
    """WebSocket connection fails with missing token."""
    client = TestClient(app)
    
    with client.websocket_connect("/ws") as websocket:
        websocket.send_json({"type": "auth", "token": ""})
        data = websocket.receive_bytes()
        assert b"Missing token" in data


def test_websocket_auth_failure_invalid_token():
    """WebSocket connection fails with invalid token."""
    client = TestClient(app)
    
    with client.websocket_connect("/ws") as websocket:
        websocket.send_json({"type": "auth", "token": "invalid"})
        data = websocket.receive_bytes()
        assert b"Unauthorized" in data or b"Invalid" in data
