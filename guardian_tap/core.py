"""Core module: attaches an observer WebSocket endpoint to any FastAPI app
and intercepts outgoing messages (WebSocket, SSE, JSON) by wrapping the middleware stack."""

from __future__ import annotations

import json
import logging
import time

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from starlette.types import ASGIApp, Receive, Scope, Send, Message

_VERSION = "0.1.0"

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


async def _broadcast_with_context(data: dict, path: str = "", method: str = "") -> None:
    """Broadcast a dict wrapped in a tap envelope with request context."""
    envelope = {
        "_tap": {
            "path": path,
            "method": method,
            "timestamp": time.time(),
        },
        **data,
    }
    await _broadcast(json.dumps(envelope))


def _try_parse_json(text: str) -> dict | None:
    """Try to parse a string as JSON dict. Returns None on failure."""
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def _extract_sse_events(body: str) -> list[dict]:
    """Extract JSON payloads from SSE body chunks."""
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
                if "type" not in parsed and current_event_type:
                    events.append({"type": current_event_type, "data": parsed})
                else:
                    events.append(parsed)
                current_event_type = None
            elif current_event_type and data_str:
                events.append({"type": current_event_type, "data": {"raw": data_str}})
                current_event_type = None

    return events


class _TapMiddleware:
    """ASGI wrapper that intercepts WebSocket sends, SSE streams, and JSON responses."""

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

        # WebSocket interception (text + binary)
        if scope_type == "websocket":
            async def ws_send_wrapper(message: Message) -> None:
                await send(message)
                if not _observers or message.get("type") != "websocket.send":
                    return

                raw = None
                if "text" in message:
                    raw = message["text"]
                elif "bytes" in message and message["bytes"]:
                    try:
                        raw = message["bytes"].decode("utf-8", errors="ignore")
                    except Exception:
                        pass

                if raw:
                    parsed = _try_parse_json(raw)
                    if parsed is not None:
                        await _broadcast_with_context(parsed, path=path, method="WS")
                    else:
                        await _broadcast(raw)

            await self.app(scope, receive, ws_send_wrapper)
            return

        # HTTP interception (SSE + JSON + chunked)
        if scope_type == "http" and _observers:
            content_type = ""
            method = scope.get("method", "")
            body_chunks: list[str] = []

            async def http_send_wrapper(message: Message) -> None:
                nonlocal content_type
                await send(message)

                # Capture content-type from response headers
                if message.get("type") == "http.response.start":
                    headers = dict(
                        (k.decode() if isinstance(k, bytes) else k,
                         v.decode() if isinstance(v, bytes) else v)
                        for k, v in message.get("headers", [])
                    )
                    content_type = headers.get("content-type", "")

                # Intercept response body
                if message.get("type") != "http.response.body" or not message.get("body"):
                    return

                chunk = message["body"]
                if isinstance(chunk, bytes):
                    chunk = chunk.decode("utf-8", errors="ignore")

                more_body = message.get("more_body", False)

                if "text/event-stream" in content_type:
                    # SSE: parse each chunk immediately
                    events = _extract_sse_events(chunk)
                    for event in events:
                        await _broadcast_with_context(event, path=path, method=method)

                elif "application/json" in content_type:
                    # JSON: may arrive in multiple chunks
                    body_chunks.append(chunk)
                    if not more_body:
                        full_body = "".join(body_chunks)
                        parsed = _try_parse_json(full_body)
                        if parsed is not None:
                            await _broadcast_with_context(parsed, path=path, method=method)
                        body_chunks.clear()

            await self.app(scope, receive, http_send_wrapper)
            return

        # Everything else: pass through
        await self.app(scope, receive, send)


def attach_observer(
    app: FastAPI,
    path: str = "/ws-observe",
) -> None:
    """Attach a GuardianAI observer endpoint to a FastAPI app.

    Intercepts WebSocket (text + binary), SSE, and JSON API responses.
    All outgoing messages are broadcast to connected observers with
    request context (path, method, timestamp).

    Args:
        app: The FastAPI application instance.
        path: The WebSocket path for observers. Defaults to "/ws-observe".

    Usage:
        from guardian_tap import attach_observer
        attach_observer(app)
    """

    # Health check endpoint (HTTP GET)
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

    # Observer WebSocket endpoint
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

    # Install middleware at startup
    @app.on_event("startup")
    async def _install_tap() -> None:
        if app.middleware_stack is None:
            app.middleware_stack = app.build_middleware_stack()
        app.middleware_stack = _TapMiddleware(app.middleware_stack, observe_path=path)
        logger.info("guardian-tap: middleware installed at %s", path)

    logger.info("guardian-tap attached at %s", path)


async def broadcast(data: dict) -> None:
    """Manually broadcast a JSON payload to all connected observers."""
    await _broadcast(json.dumps(data))
