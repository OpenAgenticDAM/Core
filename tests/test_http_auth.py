"""HTTP transport security: bearer token + allowed Host headers (for LibreChat & co.).

Runs the real Starlette app produced by `build_http_app` under uvicorn on a free port and
talks to it over real HTTP, exactly like an external MCP client would.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator

import httpx
import httpx2
import pytest
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from openagenticdam.config import Settings
from openagenticdam.http_app import MissingTokenError, build_http_app

TOKEN = "test-token-" + "x" * 40
INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
}
MCP_HEADERS = {"content-type": "application/json", "accept": "application/json, text/event-stream"}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def base_url() -> Iterator[str]:
    settings = Settings(OAD_API_TOKEN=TOKEN)  # type: ignore[call-arg]
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(build_http_app(settings), host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


def _post(base_url: str, **headers: str) -> httpx.Response:
    return httpx.post(f"{base_url}/mcp", json=INIT, headers={**MCP_HEADERS, **headers}, timeout=10)


def test_request_without_token_is_rejected(base_url):
    r = _post(base_url)
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("Bearer")


def test_request_with_wrong_token_is_rejected(base_url):
    assert _post(base_url, authorization="Bearer wrong").status_code == 401
    assert _post(base_url, authorization=TOKEN).status_code == 401  # scheme is required


def test_request_with_token_is_accepted(base_url):
    assert _post(base_url, authorization=f"Bearer {TOKEN}").status_code == 200


def test_librechat_container_host_header_is_accepted(base_url):
    port = base_url.rsplit(":", 1)[1]
    r = _post(base_url, authorization=f"Bearer {TOKEN}", host=f"host.docker.internal:{port}")
    assert r.status_code == 200


def test_unknown_host_header_is_still_rejected(base_url):
    # DNS-rebinding protection stays on for everything that is not explicitly allowed
    r = _post(base_url, authorization=f"Bearer {TOKEN}", host="evil.example:8000")
    assert r.status_code == 421


def test_healthz_needs_no_token(base_url):
    r = httpx.get(f"{base_url}/healthz", timeout=10)
    assert r.status_code in (200, 503)
    assert "status" in r.json()


async def test_mcp_client_with_bearer_header_lists_tools(base_url):
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"}, timeout=30)
    async with http, Client(streamable_http_client(f"{base_url}/mcp", http_client=http)) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert {"search_assets", "get_asset_details", "list_sources", "upload_assets"} <= names


def test_http_app_refuses_to_start_without_token():
    with pytest.raises(MissingTokenError):
        build_http_app(Settings(OAD_API_TOKEN=""))  # type: ignore[call-arg]
