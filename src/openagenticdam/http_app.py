"""Streamable-HTTP app for network clients (LibreChat, curl, other agents).

Two layers in front of the MCP endpoint:
1. Static bearer token (`OAD_API_TOKEN`), compared in constant time. The HTTP transport
   refuses to start without one - fail closed. `/healthz` stays open for container probes.
2. The SDK's DNS-rebinding protection with an explicit Host allow-list (`OAD_ALLOWED_HOSTS`),
   by default localhost plus `host.docker.internal` for clients running in Docker.

This is a development/self-hosting stopgap until OAuth (PRD: identity mapping) lands: one
token = one identity (OAD_DEV_ACTOR / OAD_DEV_PRINCIPALS).
"""

from __future__ import annotations

import hmac

from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.types import ASGIApp, Receive, Scope, Send

from openagenticdam.config import Settings
from openagenticdam.server import build_server

MIN_TOKEN_LENGTH = 32
OPEN_PATHS = frozenset({"/healthz"})


class MissingTokenError(RuntimeError):
    pass


class BearerTokenMiddleware:
    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in OPEN_PATHS:
            await self.app(scope, receive, send)
            return
        given = b""
        for name, value in scope["headers"]:
            if name == b"authorization":
                given = value
                break
        if not hmac.compare_digest(given, self.expected):
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"www-authenticate", b'Bearer realm="openagenticdam"'),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
            return
        await self.app(scope, receive, send)


def _patterns(hosts: list[str]) -> tuple[list[str], list[str]]:
    allowed_hosts = hosts
    allowed_origins = [f"http://{h}" for h in hosts] + [f"https://{h}" for h in hosts]
    return allowed_hosts, allowed_origins


def build_http_app(settings: Settings) -> Starlette:
    token = settings.api_token.strip()
    if len(token) < MIN_TOKEN_LENGTH:
        raise MissingTokenError(
            f"OAD_API_TOKEN must be set (>= {MIN_TOKEN_LENGTH} chars) to serve over HTTP; run scripts/init-env.sh"
        )
    hosts, origins = _patterns([h.strip() for h in settings.allowed_hosts.split(",") if h.strip()])
    mcp = build_server(settings, client_name="http")
    app = mcp.streamable_http_app(
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=hosts,
            allowed_origins=origins,
        ),
    )
    app.add_middleware(BearerTokenMiddleware, token=token)
    return app
