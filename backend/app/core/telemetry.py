"""Request correlation and payload-free operational events."""
import json
import logging
from contextvars import ContextVar
from time import monotonic
from uuid import uuid4

from starlette.datastructures import MutableHeaders

from app.core.logging import get_logger

logger = get_logger(__name__)
logger.addHandler(logging.StreamHandler())
logger.setLevel(logging.INFO)
logger.propagate = False
request_id = ContextVar("request_id", default=None)


def event(name: str, **measurements) -> None:
    logger.info(json.dumps({"event": name, "request_id": request_id.get(), **measurements}))


class RequestTelemetry:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        token = request_id.set(uuid4().hex)
        start = monotonic()
        status = 500

        async def observe(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message)["X-Request-ID"] = request_id.get()
            await send(message)

        try:
            await self.app(scope, receive, observe)
        finally:
            route = getattr(scope.get("route"), "path", None)
            event("http_request", route=route, method=scope["method"], status=status,
                  duration_ms=round((monotonic() - start) * 1000))
            request_id.reset(token)
