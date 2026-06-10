"""WebSocket server for real-time dashboard data streaming."""

from __future__ import annotations

import json
import logging
from typing import List
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from polyagent.agents.base import signal_bus

logger = logging.getLogger(__name__)
router = APIRouter(tags=["websocket"])


class ConnectionManager:
    """Manages active WebSocket connections and handles broadcasts."""

    def __init__(self) -> None:
        self.active_connections: List[WebSocket] = []
        self._subscribed = False

    async def connect(self, websocket: WebSocket) -> None:
        """Accepts a new connection and registers it."""
        await websocket.accept()
        self.active_connections.append(websocket)
        logger.debug("New WebSocket client connected. Active: %d", len(self.active_connections))
        
        # Subscribe to signal bus events on first connection
        if not self._subscribed:
            self._setup_event_listeners()
            self._subscribed = True

    def disconnect(self, websocket: WebSocket) -> None:
        """Removes a disconnected client."""
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
            logger.debug("WebSocket client disconnected. Active: %d", len(self.active_connections))

    async def send_personal_message(self, message: dict, websocket: WebSocket) -> None:
        """Sends a JSON message to a specific connection."""
        await websocket.send_json(message)

    async def broadcast(self, message: dict) -> None:
        """Broadcasts a JSON message to all connected clients in parallel."""
        if not self.active_connections:
            return

        logger.debug("Broadcasting WebSocket message: %s", message.get("type"))
        
        # Make a copy of connections to avoid modification errors during loop
        connections = list(self.active_connections)
        for connection in connections:
            try:
                await connection.send_json(message)
            except Exception:
                self.disconnect(connection)

    # ── Signal Bus Subscription ───────────────────────────────────

    def _setup_event_listeners(self) -> None:
        """Subscribes the WebSocket manager to background agent events."""
        logger.info("Hooking WebSocket broadcaster into internal agent event bus")
        
        # Helper wrapper to schedule async broadcast from sync-like signatures
        import asyncio

        async def ws_broadcast_handler(topic: str, data: Any) -> None:
            payload = {
                "type": topic,
                "timestamp": datetime.now(timezone.utc).isoformat() if "datetime" in globals() else "",
                "data": data
            }
            # Retrieve running event loop or fallback
            try:
                loop = asyncio.get_running_loop()
                if loop.is_running():
                    loop.create_task(self.broadcast(payload))
            except RuntimeError:
                pass

        # Register callbacks for topics on the global bus
        signal_bus.subscribe("signal_detected", lambda d: ws_broadcast_handler("signal_detected", d))
        signal_bus.subscribe("signal_approved", lambda d: ws_broadcast_handler("signal_approved", d))
        signal_bus.subscribe("paper_order_filled", lambda d: ws_broadcast_handler("paper_order_filled", d))
        signal_bus.subscribe("system_alert", lambda d: ws_broadcast_handler("system_alert", d))


# Module-level connection manager instance
manager = ConnectionManager()


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """FastAPI WebSocket endpoint for the frontend dashboard."""
    await manager.connect(websocket)
    try:
        # Keep connection open and listen for heartbeats / client requests
        while True:
            data = await websocket.receive_text()
            try:
                # Handle potential client commands (e.g. ping, request_state)
                msg = json.loads(data)
                if msg.get("type") == "ping":
                    await websocket.send_json({"type": "pong"})
            except Exception:
                pass
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        logger.error("Error in WebSocket handler: %s", e)
        manager.disconnect(websocket)
