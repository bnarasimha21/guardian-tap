"""Core module: attaches an observer WebSocket endpoint to any FastAPI app
and patches active WebSocket connections to broadcast all sent messages."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

logger = logging.getLogger("guardian_tap")

# Global set of observer connections
_observers: set[WebSocket] = set()


async def _broadcast_to_observers(data: dict) -> None:
    """Send a JSON payload to all connected observers. Removes dead connections."""
    if not _observers:
        return
    dead: set[WebSocket] = set()
    for obs in _observers:
        try:
            await obs.send_json(data)
        except Exception:
            dead.add(obs)
    _observers.difference_update(dead)


def _patch_websocket(ws: WebSocket) -> WebSocket:
    """Wrap a WebSocket's send_json to also broadcast to observers."""
    original_send_json = ws.send_json

    async def patched_send_json(data: Any, mode: str = "text") -> None:
        await original_send_json(data, mode=mode)
        if isinstance(data, dict):
            await _broadcast_to_observers(data)

    ws.send_json = patched_send_json  # type: ignore[assignment]
    return ws


def attach_observer(
    app: FastAPI,
    path: str = "/ws-observe",
    auto_patch: bool = True,
) -> None:
    """Attach a GuardianAI observer endpoint to a FastAPI app.

    Args:
        app: The FastAPI application instance.
        path: The WebSocket path for observers to connect to.
              Defaults to "/ws-observe".
        auto_patch: If True, automatically patches all incoming WebSocket
                    connections to broadcast messages to observers.
                    If False, you must call guardian_tap.broadcast() manually.

    Usage:
        from guardian_tap import attach_observer
        attach_observer(app)
    """

    # 1. Add the observer WebSocket endpoint
    @app.websocket(path)
    async def _guardian_observe(websocket: WebSocket) -> None:
        await websocket.accept()
        _observers.add(websocket)
        logger.info("Guardian observer connected (%d total)", len(_observers))
        try:
            while True:
                await websocket.receive_text()  # keep-alive, ignore input
        except WebSocketDisconnect:
            _observers.discard(websocket)
            logger.info("Guardian observer disconnected (%d remaining)", len(_observers))

    # 2. If auto_patch, wrap the app's WebSocket handler via middleware
    if auto_patch:
        _install_ws_middleware(app)

    logger.info("guardian-tap attached at %s (auto_patch=%s)", path, auto_patch)


def _install_ws_middleware(app: FastAPI) -> None:
    """Install ASGI middleware that patches WebSocket connections."""

    original_app = app.router.app  # the inner ASGI app

    async def middleware(scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "websocket" and scope["path"] != "/ws-observe":
            # Wrap the send callable to intercept outgoing messages
            async def patched_send(message: dict) -> None:
                await send(message)
                # Intercept websocket.send messages with JSON text
                if message.get("type") == "websocket.send" and "text" in message:
                    try:
                        import json
                        data = json.loads(message["text"])
                        if isinstance(data, dict):
                            await _broadcast_to_observers(data)
                    except (json.JSONDecodeError, TypeError):
                        pass

            await original_app(scope, receive, patched_send)
        else:
            await original_app(scope, receive, send)

    app.router.app = middleware  # type: ignore[assignment]


async def broadcast(data: dict) -> None:
    """Manually broadcast a JSON payload to all connected observers.

    Use this when auto_patch=False, or to send custom events:
        import guardian_tap
        await guardian_tap.broadcast({"type": "custom_event", "data": {...}})
    """
    await _broadcast_to_observers(data)
