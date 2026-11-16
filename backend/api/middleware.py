"""Request-ID middleware.

Gives every request an ID, attaches it to all log lines for that request, and
returns it in the ``X-Request-ID`` response header so a user-reported error can
be matched to its logs.
"""

import json
import re
import time
import uuid

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

HEADER = b"x-request-id"
# A caller-supplied ID is only trusted if it is short and plain. Anything else is
# replaced, so a client cannot inject arbitrary text into our logs.
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

log = structlog.get_logger("eia.request")


def _incoming_request_id(scope: Scope) -> str | None:
    for name, value in scope.get("headers", []):
        if name == HEADER:
            candidate: str = value.decode("latin-1")
            return candidate if _SAFE_ID.fullmatch(candidate) else None
    return None


async def _send_internal_error(send: Send, request_id: str) -> None:
    body = json.dumps(
        {
            "type": "about:blank",
            "title": "Internal Server Error",
            "status": 500,
            "detail": "Something went wrong. Quote the request ID if you report this.",
            "request_id": request_id,
        }
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 500,
            "headers": [
                (b"content-type", b"application/problem+json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class RequestIdMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _incoming_request_id(scope) or uuid.uuid4().hex
        status_code = 500
        response_started = False
        started = time.perf_counter()

        async def send_with_header(message: Message) -> None:
            nonlocal status_code, response_started
            if message["type"] == "http.response.start":
                status_code = message["status"]
                response_started = True
                message["headers"] = [*message.get("headers", []), (HEADER, request_id.encode())]
            await send(message)

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        try:
            await self.app(scope, receive, send_with_header)
        except Exception:
            # An unhandled error. Log the traceback here, where the request ID is
            # still bound, and answer with a generic 500: the client gets the ID
            # to quote, never the exception text.
            log.exception("unhandled_error", method=scope["method"], path=scope["path"])
            if response_started:
                raise
            status_code = 500
            await _send_internal_error(send_with_header, request_id)
        finally:
            log.info(
                "request",
                method=scope["method"],
                path=scope["path"],
                status=status_code,
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
            )
            structlog.contextvars.clear_contextvars()
