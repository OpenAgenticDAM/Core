"""Smoke test against the running compose stack over Streamable HTTP.

    uv run python scripts/smoke.py

Uploads three generated demo images, ingests them through the Celery worker container,
then calls all three MCP tools via http://127.0.0.1:8000/mcp and fetches one thumbnail.
"""

from __future__ import annotations

import asyncio
import io
import shutil
import subprocess
import sys
import time

import httpx
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from PIL import Image

from openagenticdam.config import Settings
from openagenticdam.db import connect
from openagenticdam.storage import s3_client

DEMO = {
    "demo/red_motorcycle_on_mountain_road.jpg": (200, 30, 30),
    "demo/blue_sea_beach_summer.jpg": (40, 90, 210),
    "demo/green_forest_hiking_trail.jpg": (40, 150, 60),
}


def main() -> int:
    s = Settings()
    s3 = s3_client(s)
    for key, rgb in DEMO.items():
        buf = io.BytesIO()
        Image.new("RGB", (1200, 800), rgb).save(buf, "JPEG")
        s3.put_object(Bucket=s.s3_bucket, Key=key, Body=buf.getvalue(), ContentType="image/jpeg")
    print(f"uploaded {len(DEMO)} objects to s3://{s.s3_bucket}/demo/")

    docker = shutil.which("docker")
    if docker is None:
        print("FAIL: docker CLI not found")
        return 1
    out = subprocess.run(  # noqa: S603 - fixed argv, resolved executable
        [docker, "compose", "exec", "-T", "gateway", "openagenticdam", "ingest", "--prefix", "demo/", "--async"],
        check=True,
        capture_output=True,
        text=True,
    )
    print(out.stdout.strip())

    deadline = time.time() + 60
    with connect(s) as conn:
        while True:
            row = conn.execute(
                "SELECT count(*) FROM assets a JOIN embeddings e ON e.asset_id = a.id"
                " WHERE a.tenant_id = %s AND a.external_id LIKE 'demo/%%'",
                (s.tenant_id,),
            ).fetchone()
            n = row[0] if row else 0
            if n >= len(DEMO):
                break
            if time.time() > deadline:
                print(f"FAIL: only {n}/{len(DEMO)} assets ingested by the worker")
                return 1
            time.sleep(1)
    print(f"worker ingested {n}/{len(DEMO)} assets")

    async def mcp_calls() -> int:
        http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {s.api_token}"}, timeout=60)
        async with http, Client(streamable_http_client("http://127.0.0.1:8000/mcp", http_client=http)) as client:
            tools = sorted(t.name for t in (await client.list_tools()).tools)
            print("tools:", tools)
            src = (await client.call_tool("list_sources", {})).structured_content
            print("sources:", [(x["name"], x["asset_count"]) for x in src["sources"]])
            res = await client.call_tool("search_assets", {"query": "Motorrad in den Bergen", "limit": 3})
            hits = res.structured_content["hits"]
            for h in hits:
                print(f"  {h['score']:.3f}  {h['file_name']}")
            if not hits or "motorcycle" not in hits[0]["file_name"]:
                print("FAIL: expected the motorcycle image as top hit")
                return 1
            det = (await client.call_tool("get_asset_details", {"asset_id": hits[0]["asset_id"]})).structured_content
            print(
                "details:",
                det["technical_metadata"]["width"],
                "x",
                det["technical_metadata"]["height"],
                det["technical_metadata"]["colours"][0],
            )
            r = httpx.get(hits[0]["thumbnail_url"], timeout=5)
            print("thumbnail:", r.status_code, r.headers.get("content-type"), len(r.content), "bytes")
            return 0 if r.status_code == 200 else 1

    return asyncio.run(mcp_calls())


if __name__ == "__main__":
    sys.exit(main())
