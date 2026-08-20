import os

os.environ.setdefault("AUTH_TOKEN", "test-token")

import asyncio
import time

import pytest
from httpx import ASGITransport, AsyncClient

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
async def client():
    from app.main import app, bucket, store

    store.jobs.clear()
    store._cache.clear()
    store._idempotency.clear()
    bucket.tokens = bucket.capacity

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", timeout=60) as c:
        yield c


def file_section(path: str, lines: list[str], start: int = 1) -> str:
    body = "".join(f"+{line}\n" for line in lines)
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +{start},{len(lines)} @@\n"
        f"{body}"
    )


async def submit(client, diff, expect=202, **options):
    body = {"diff": diff}
    if options:
        body["options"] = options
    resp = await client.post("/v1/reviews", json=body, headers=AUTH)
    assert resp.status_code == expect, resp.text
    return resp.json()["jobId"]


async def wait_done(client, job_id, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = await client.get(f"/v1/reviews/{job_id}", headers=AUTH)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        if body["status"] in ("done", "failed"):
            return body
        await asyncio.sleep(0.01)
    raise AssertionError(f"job {job_id} did not finish within {timeout}s")


async def read_stream(client, job_id) -> str:
    chunks = []
    async with client.stream("GET", f"/v1/reviews/{job_id}/stream", headers=AUTH) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        async for piece in resp.aiter_text():
            chunks.append(piece)
    return "".join(chunks)


def parse_sse(raw: str) -> list[tuple[str, str]]:
    events = []
    for block in raw.strip().split("\n\n"):
        if not block.strip():
            continue
        name = data = None
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = line[6:]
        events.append((name, data))
    return events
