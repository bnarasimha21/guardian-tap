"""Core module: attaches an observer WebSocket endpoint to any FastAPI app
and intercepts outgoing WebSocket messages by wrapping the middleware stack."""

from __future__ import annotations

import json
import logging

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from starlette.types import ASGIApp, Receive, Scope, Send, Message

logger = logging.getLogger("guardian_tap")

_observers: set[WebSocket] = set()


async def _broadcast(text: str) -> None:
    """Broadcast a raw JSON text to all observers."""
    if not _observers:
        return
    dead: set[WebSocket] = set()
    for obs in _observers:
        try:
            await obs.send_text(text)
        except Exception:
            dead.add(obs)
    _observers.difference_update(dead)


class _TapMiddleware:
    """ASGI wrapper that intercepts WebSocket sends and broadcasts to observers."""

    def __init__(self, app: ASGIApp, observe_path: str) -> None:
        self.app = app
        self.observe_path = observe_path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") == "websocket" and scope.get("path") != self.observe_path:
            async def send_wrapper(message: Message) -> None:
                await send(message)
                if (
                    message.get("type") == "websocket.send"
                    and "text" in message
                    and _observers
                ):
                    await _broadcast(message["text"])

            await self.app(scope, receive, send_wrapper)
        else:
            await self.app(scope, receive, send)


def attach_observer(
    app: FastAPI,
    path: str = "/ws-observe",
) -> None:
    """Attach a GuardianAI observer endpoint to a FastAPI app.

    Args:
        app: The FastAPI application instance.
        path: The WebSocket path for observers. Defaults to "/ws-observe".

    Usage:
        from guardian_tap import attach_observer
        attach_observer(app)
    """

    # 1. Add the observer endpoint
    @app.websocket(path)
    async def _guardian_observe(websocket: WebSocket) -> None:
        await websocket.accept()
        _observers.add(websocket)
        logger.info("Guardian observer connected (%d total)", len(_observers))
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            _observers.discard(websocket)
            logger.info("Guardian observer disconnected (%d remaining)", len(_observers))

    # 2. Wrap the middleware stack at startup to intercept WebSocket sends.
    #    We do this in a startup event because Starlette builds the stack lazily.
    @app.on_event("startup")
    async def _install_tap() -> None:
        # Force the middleware stack to build if it hasn't yet
        if app.middleware_stack is None:
            app.middleware_stack = app.build_middleware_stack()
        # Wrap it with our interceptor
        app.middleware_stack = _TapMiddleware(app.middleware_stack, observe_path=path)
        logger.info("guardian-tap: middleware installed at %s", path)

    logger.info("guardian-tap attached at %s", path)


async def broadcast(data: dict) -> None:
    """Manually broadcast a JSON payload to all connected observers."""
    await _broadcast(json.dumps(data))
