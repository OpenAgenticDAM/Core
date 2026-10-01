"""Minimal MCP Apps host for a real-browser test of ui/upload.html.

Serves a host page that embeds the app in a sandboxed iframe (like Claude Desktop) and proxies
the app's JSON-RPC (ui/initialize, tools/call, ui/message, ...) to the real MCP server over
Streamable HTTP. Run:  uv run python scripts/dev_app_host.py  -> http://127.0.0.1:8765
"""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager

import uvicorn
from mcp import Client
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

MCP_URL = "http://127.0.0.1:8000/mcp"
UI_URI = "ui://openagenticdam/upload.html"
LOG: list[dict] = []

HOST_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>dev MCP Apps host</title>
<style>body{font-family:system-ui;margin:16px;background:#171717;color:#eee}iframe{width:560px;border:1px solid #444;border-radius:8px;background:#1a1a1a}
pre{font-size:11px;max-height:200px;overflow:auto;background:#000;padding:8px}</style></head><body>
<h3>dev MCP Apps host</h3><iframe id="app" sandbox="allow-scripts" height="200"></iframe>
<h4>ui/message (Chat)</h4><pre id="chat"></pre>
<script>
const frame = document.getElementById("app");
const chat = document.getElementById("chat");
let initialized = false;
fetch("/resource").then(r => r.text()).then(html => { frame.srcdoc = html; });
window.addEventListener("message", async (ev) => {
  if (ev.source !== frame.contentWindow) return;
  const m = ev.data; if (!m || m.jsonrpc !== "2.0") return;
  const reply = (body) => frame.contentWindow.postMessage({jsonrpc: "2.0", id: m.id, ...body}, "*");
  if (m.method === "ui/initialize") {
    reply({result: {protocolVersion: "2026-01-26", hostInfo: {name: "dev-host", version: "0"},
      hostCapabilities: {serverTools: {}, openLinks: {}},
      hostContext: {theme: "dark", displayMode: "inline", styles: {variables: {"--color-text-info": "#5b9bff"}}}}});
  } else if (m.method === "ui/notifications/initialized") {
    initialized = true;
    const r = await fetch("/rpc", {method: "POST", body: JSON.stringify({method: "tools/call", params: {name: "upload_assets", arguments: {}}})}).then(r => r.json());
    frame.contentWindow.postMessage({jsonrpc: "2.0", method: "ui/notifications/tool-input", params: {arguments: {}}}, "*");
    frame.contentWindow.postMessage({jsonrpc: "2.0", method: "ui/notifications/tool-result", params: r.result}, "*");
  } else if (m.method === "ui/notifications/size-changed") {
    if (m.params.height) frame.height = m.params.height;
  } else if (m.method === "ui/message") {
    chat.textContent += m.params.content.text + "\\n"; reply({result: {}});
  } else if (m.method === "ui/update-model-context") {
    window.__modelContext = m.params; reply({result: {}});
  } else if (m.id !== undefined) {
    if (!initialized) { reply({error: {code: -32000, message: "not initialized"}}); return; }
    const r = await fetch("/rpc", {method: "POST", body: JSON.stringify({method: m.method, params: m.params})}).then(r => r.json());
    reply(r);
  }
});
</script></body></html>"""


@asynccontextmanager
async def lifespan(app: Starlette):
    async with Client(MCP_URL) as client:
        app.state.client = client
        tools = {t.name: t for t in (await client.list_tools()).tools}
        app.state.app_only = {n for n, t in tools.items() if (t.meta or {}).get("ui", {}).get("visibility") == ["app"]}
        yield


async def page(_: Request) -> HTMLResponse:
    return HTMLResponse(HOST_PAGE)


async def resource(request: Request) -> HTMLResponse:
    res = await request.app.state.client.read_resource(UI_URI)
    return HTMLResponse(res.contents[0].text)


async def rpc(request: Request) -> JSONResponse:
    body = json.loads(await request.body())
    client: Client = request.app.state.client
    if body["method"] != "tools/call":
        return JSONResponse({"error": {"code": -32601, "message": "not proxied"}})
    p = body["params"]
    res = await client.call_tool(p["name"], p.get("arguments") or {})
    out = res.model_dump(mode="json", by_alias=True, exclude_none=True)
    LOG.append({"id": str(uuid.uuid4())[:8], "tool": p["name"], "isError": out.get("isError", False)})
    return JSONResponse({"result": out})


async def log(_: Request) -> JSONResponse:
    return JSONResponse(LOG)


app = Starlette(
    routes=[Route("/", page), Route("/resource", resource), Route("/rpc", rpc, methods=["POST"]), Route("/log", log)],
    lifespan=lifespan,
)

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")
