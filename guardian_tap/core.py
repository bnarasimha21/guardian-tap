"""Core module: attaches an observer WebSocket endpoint to any FastAPI app
and intercepts outgoing messages (WebSocket and SSE) by wrapping the middleware stack."""

from __future__ import annotations

import json
import logging
import re

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from starlette.types import ASGIApp, Receive, Scope, Send, Message

_VERSION = "0.1.0"

logger = logging.getLogger("guardian_tap")

_observers: set[WebSocket] = set()

# Regex to parse SSE "data:" lines
_SSE_DATA_RE = re.compile(r"^data:\s*(.+)$", re.MULTILINE)


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


def _try_parse_json(text: str) -> dict | None:
    """Try to parse a string as JSON dict. Returns None on failure."""
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def _extract_sse_events(body: str) -> list[dict]:
    """Extract JSON payloads from SSE body chunks.

    SSE format:
        event: some_event
        data: {"key": "value"}

    We extract the data lines and parse them as JSON.
    We also capture the event type from preceding "event:" lines.
    """
    events = []
    current_event_type = None

    for line in body.split("\n"):
        line = line.strip()
        if line.startswith("event:"):
            current_event_type = line[6:].strip()
        elif line.startswith("data:"):
            data_str = line[5:].strip()
            parsed = _try_parse_json(data_str)
            if parsed is not None:
                # Wrap in a standard envelope if it doesn't have "type"
                if "type" not in parsed and current_event_type:
                    events.append({
                        "type": current_event_type,
                        "data": parsed,
                    })
                else:
                    events.append(parsed)
                current_event_type = None
            elif current_event_type and data_str:
                # Non-JSON data line
                events.append({
                    "type": current_event_type,
                    "data": {"raw": data_str},
                })
                current_event_type = None

    return events


class _TapMiddleware:
    """ASGI wrapper that intercepts WebSocket sends and SSE responses,
    broadcasting them to connected observers."""

    def __init__(self, app: ASGIApp, observe_path: str) -> None:
        self.app = app
        self.observe_path = observe_path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        scope_type = scope.get("type", "")
        path = scope.get("path", "")

        # Skip the observer endpoint itself
        if path == self.observe_path:
            await self.app(scope, receive, send)
            return

        # WebSocket interception
        if scope_type == "websocket":
            async def ws_send_wrapper(message: Message) -> None:
                await send(message)
                if (
                    message.get("type") == "websocket.send"
                    and "text" in message
                    and _observers
                ):
                    await _broadcast(message["text"])

            await self.app(scope, receive, ws_send_wrapper)
            return

        # HTTP interception (for SSE streams)
        if scope_type == "http" and _observers:
            is_sse = False

            async def http_send_wrapper(message: Message) -> None:
                nonlocal is_sse
                await send(message)

                # Detect SSE from response headers
                if message.get("type") == "http.response.start":
                    headers = dict(
                        (k.decode() if isinstance(k, bytes) else k,
                         v.decode() if isinstance(v, bytes) else v)
                        for k, v in message.get("headers", [])
                    )
                    content_type = headers.get("content-type", "")
                    is_sse = "text/event-stream" in content_type

                # Intercept SSE body chunks
                if (
                    is_sse
                    and message.get("type") == "http.response.body"
                    and message.get("body")
                ):
                    body = message["body"]
                    if isinstance(body, bytes):
                        body = body.decode("utf-8", errors="ignore")

                    events = _extract_sse_events(body)
                    for event in events:
                        await _broadcast(json.dumps(event))

            await self.app(scope, receive, http_send_wrapper)
            return

        # Everything else: pass through
        await self.app(scope, receive, send)


def attach_observer(
    app: FastAPI,
    path: str = "/ws-observe",
) -> None:
    """Attach a GuardianAI observer endpoint to a FastAPI app.

    Supports both WebSocket and SSE (Server-Sent Events) apps.
    All outgoing messages are broadcast to connected observers.

    Args:
        app: The FastAPI application instance.
        path: The WebSocket path for observers. Defaults to "/ws-observe".

    Usage:
        from guardian_tap import attach_observer
        attach_observer(app)
    """

    # 1a. Add HTTP health check at the same path
    @app.get(path)
    async def _guardian_health():
        ws_available = True
        try:
            import websockets  # noqa: F401
        except ImportError:
            ws_available = False
        return {
            "status": "ok",
            "guardian_tap": True,
            "version": _VERSION,
            "framework": "fastapi",
            "observers": len(_observers),
            "websocket_support": ws_available,
        }

    # 1b. Add the observer WebSocket endpoint
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

    # 2. Wrap the middleware stack at startup
    @app.on_event("startup")
    async def _install_tap() -> None:
        if app.middleware_stack is None:
            app.middleware_stack = app.build_middleware_stack()
        app.middleware_stack = _TapMiddleware(app.middleware_stack, observe_path=path)
        logger.info("guardian-tap: middleware installed at %s (ws + sse)", path)

    logger.info("guardian-tap attached at %s", path)


async def broadcast(data: dict) -> None:
    """Manually broadcast a JSON payload to all connected observers."""
    await _broadcast(json.dumps(data))
