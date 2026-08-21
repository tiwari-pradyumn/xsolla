"""The safety nets.

Every path here exists so that something going wrong degrades into a `failed`
job or an error envelope instead of taking the process down. They are the
hardest paths to reach by accident, which is exactly why they are worth pinning:
an untested safety net is a claim, not a guarantee.
"""

import asyncio

import pytest
from fastapi.exceptions import RequestValidationError
from httpx import ASGITransport, AsyncClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.providers.base import ProviderError
from tests.conftest import AUTH, file_section, submit, wait_done

SIMPLE = file_section("src/a.ts", ["eval(x);"])


# --- a scan that goes wrong ---------------------------------------------


async def test_an_unexpected_exception_fails_the_job_not_the_process(client, monkeypatch):
    """A provider bug is not a ProviderError: it is unforeseen. The job still
    fails cleanly, names the exception type so the bug is diagnosable, and the
    service keeps serving."""
    import app.jobs as jobs_module

    class Buggy:
        name = "mock"

        async def scan(self, files):
            raise ZeroDivisionError("division by zero")

    monkeypatch.setattr(jobs_module, "get_provider", lambda name: Buggy())

    body = await wait_done(client, await submit(client, SIMPLE))
    assert body["status"] == "failed"
    assert body["error"]["code"] == "internal"
    assert "ZeroDivisionError" in body["error"]["message"]
    assert "findings" not in body

    assert (await client.get("/health")).status_code == 200


async def test_an_unexpected_failure_is_not_cached(client, monkeypatch):
    """The cache entry is claimed before the scan runs, so a failure has to
    withdraw it -- otherwise one transient bug would be served forever."""
    import app.jobs as jobs_module

    calls = []
    real = jobs_module.get_provider

    class FailsOnce:
        name = "mock"

        async def scan(self, files):
            calls.append(1)
            if len(calls) == 1:
                raise ZeroDivisionError("division by zero")
            return await real("mock").scan(files)

    monkeypatch.setattr(jobs_module, "get_provider", lambda name: FailsOnce())

    assert (await wait_done(client, await submit(client, SIMPLE)))["status"] == "failed"

    retried = await wait_done(client, await submit(client, SIMPLE))
    assert len(calls) == 2, "the failed job was cached and never rescanned"
    assert retried["status"] == "done"
    assert retried["usage"]["cacheHit"] is False


async def test_a_coalesced_job_inherits_the_failure_it_waited_on(client, monkeypatch):
    """A duplicate submitted while the original is still running performs no
    scan of its own, so when the original fails there is nothing to fall back
    on: it must report the same failure rather than hang or report success."""
    import app.jobs as jobs_module

    scanning = asyncio.Event()
    release = asyncio.Event()

    class Stalls:
        name = "mock"

        async def scan(self, files):
            scanning.set()
            await release.wait()
            raise ProviderError("model is unreachable")

    monkeypatch.setattr(jobs_module, "get_provider", lambda name: Stalls())

    first_id = await submit(client, SIMPLE)
    await asyncio.wait_for(scanning.wait(), timeout=5)
    second_id = await submit(client, SIMPLE)  # coalesces onto the running job
    release.set()

    first = await wait_done(client, first_id)
    second = await wait_done(client, second_id)

    assert first["status"] == "failed"
    assert second["status"] == "failed"
    assert second["usage"]["cacheHit"] is True
    assert second["error"] == first["error"]


async def test_the_stream_of_a_coalesced_failure_terminates(client, monkeypatch):
    """The waiting job's event log must close too, or an SSE consumer attached
    to it never sees the connection end."""
    import app.jobs as jobs_module

    from tests.conftest import parse_sse, read_stream

    class Fails:
        name = "mock"

        async def scan(self, files):
            raise ProviderError("model is unreachable")

    monkeypatch.setattr(jobs_module, "get_provider", lambda name: Fails())

    await wait_done(client, await submit(client, SIMPLE))
    second_id = await submit(client, SIMPLE)
    await wait_done(client, second_id)

    events = parse_sse(await read_stream(client, second_id))
    assert [name for name, _ in events][-2:] == ["status", "done"]
    assert "failed" in events[-2][1]


# --- state that cannot be reached through the API today ------------------


def test_a_dangling_idempotency_key_is_treated_as_fresh():
    """Nothing evicts jobs today (ADR-0002), so this branch is unreachable
    through the API. It is the one line that would decide the contract if
    retention ever changed, so it is pinned here rather than left to be
    rediscovered: a key pointing at a job that no longer exists must not
    resurrect a 404 jobId."""
    from app.jobs import JobStore

    store = JobStore()
    store._idempotency["k"] = ("body-hash", "a-job-that-is-gone")

    assert store.idempotent_replay("k", "body-hash") is None
    assert "k" not in store._idempotency


async def test_non_http_scopes_pass_through_the_auth_gate():
    """The gate guards HTTP requests. A lifespan scope carries no path and no
    headers; treating it as an unauthenticated request would break startup."""
    from app.main import V1AuthGate

    seen = []

    async def inner(scope, receive, send):
        seen.append(scope["type"])

    await V1AuthGate(inner)({"type": "lifespan"}, None, None)
    assert seen == ["lifespan"]


# --- the exception handlers ---------------------------------------------


async def test_an_unhandled_exception_becomes_the_error_envelope(monkeypatch):
    """The last line of defence. It needs its own client because the default
    test transport re-raises whatever escaped, which would hide the response
    the caller actually received."""
    from app.main import app, store

    def boom(_job_id):
        raise RuntimeError("something nobody predicted")

    monkeypatch.setattr(store, "get", boom)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/v1/reviews/anything", headers=AUTH)

    assert resp.status_code == 500
    assert resp.json() == {"error": {"code": "internal", "message": "Internal server error."}}
    assert "something nobody predicted" not in resp.text, "internals leaked to the caller"


@pytest.mark.parametrize(
    "exc,status,code",
    [
        (StarletteHTTPException(404), 404, "not_found"),
        (StarletteHTTPException(405), 404, "not_found"),
        (StarletteHTTPException(401), 401, "unauthorized"),
        (StarletteHTTPException(503, "upstream is down"), 503, "internal"),
    ],
)
async def test_every_http_exception_maps_onto_a_published_code(exc, status, code):
    """Exercised at the registry, because no route raises these: auth is an ASGI
    gate and 404/405 are resolved by the router. The mapping still has to hold
    for anything that ever does raise one."""
    from app.main import app

    handler = app.exception_handlers[StarletteHTTPException]
    resp = await handler(None, exc)

    assert resp.status_code == status
    assert resp.body.decode().startswith(f'{{"error":{{"code":"{code}"')


async def test_a_request_validation_error_maps_onto_a_published_code():
    """No route declares a validated model today, so nothing raises this. The
    handler exists because the taxonomy has no code for FastAPI's own 422
    shape, and this pins the substitution it makes."""
    from app.main import app

    handler = app.exception_handlers[RequestValidationError]
    resp = await handler(None, RequestValidationError([]))

    assert resp.status_code == 400
    assert b'"code":"invalid_json"' in resp.body
