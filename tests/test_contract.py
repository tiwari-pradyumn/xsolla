"""Public endpoints, auth on every /v1 route, and the error taxonomy."""

import json

import pytest

from app.config import MAX_PAYLOAD_BYTES, VERSION
from tests.conftest import AUTH, file_section, submit

SIMPLE = file_section("src/a.ts", ["eval(x);"])


async def test_health(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["version"].count(".") == 2
    assert isinstance(body["uptimeSeconds"], (int, float))


async def test_spec_matches_declared_limits(client):
    resp = await client.get("/spec")
    assert resp.status_code == 200
    assert resp.json() == {
        "specVersion": "1.0",
        "providers": ["mock", "llm"],
        "limits": {
            "maxPayloadBytes": 1048576,
            "chunkBytes": 65536,
            "maxConcurrentJobs": 4,
            "rateLimitPerMinute": 30,
        },
    }


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/v1/reviews"),
        ("GET", "/v1/reviews/anything"),
        ("GET", "/v1/reviews/anything/stream"),
    ],
)
@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer wrong"}, {"Authorization": "wrong"}, {"Authorization": "Bearer "}],
)
async def test_all_v1_routes_require_auth(client, method, path, headers):
    resp = await client.request(method, path, headers=headers, json={"diff": SIMPLE})
    assert resp.status_code == 401
    assert resp.json() == {
        "error": {"code": "unauthorized", "message": "Missing or invalid bearer token."}
    }


async def test_public_routes_need_no_auth(client):
    assert (await client.get("/health")).status_code == 200
    assert (await client.get("/spec")).status_code == 200


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/v1/unknown"),
        ("GET", "/v1/reviews"),
        ("PUT", "/v1/reviews"),
        ("DELETE", "/v1/reviews/anything"),
        ("PATCH", "/v1/reviews/anything"),
        ("POST", "/v1/reviews/anything/stream"),
    ],
)
async def test_auth_precedes_routing_under_v1(client, method, path):
    """The gate is on the prefix, not the handlers: a path that does not exist
    and a method that is not allowed are both unauthorized before anything else.
    Routing these first would leak which /v1 routes exist to an anonymous caller."""
    resp = await client.request(method, path)
    assert resp.status_code == 401, f"{method} {path}"
    assert resp.json()["error"]["code"] == "unauthorized"


@pytest.mark.parametrize(
    "method,path",
    [
        ("PUT", "/v1/reviews"),
        ("DELETE", "/v1/reviews/anything"),
        ("GET", "/v1/unknown"),
    ],
)
async def test_authenticated_bad_route_is_404_not_405(client, method, path):
    """405 collapses into 404 so that every status the service emits maps onto a
    code the task published; the taxonomy has no code for a wrong method."""
    resp = await client.request(method, path, headers=AUTH)
    assert resp.status_code == 404, f"{method} {path}"
    assert resp.json()["error"]["code"] == "not_found"


async def test_invalid_json_is_400(client):
    resp = await client.post(
        "/v1/reviews", content=b"{not json", headers={**AUTH, "Content-Type": "application/json"}
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_json"


@pytest.mark.parametrize(
    "body",
    [{}, {"diff": ""}, {"diff": "   "}, {"diff": "not a diff at all"}, {"diff": 42}],
)
async def test_unusable_diff_is_422(client, body):
    resp = await client.post("/v1/reviews", json=body, headers=AUTH)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_diff"


async def test_oversized_payload_is_413(client):
    padding = "x" * (MAX_PAYLOAD_BYTES + 1024)
    body = json.dumps({"diff": SIMPLE + padding}).encode()
    resp = await client.post(
        "/v1/reviews", content=body, headers={**AUTH, "Content-Type": "application/json"}
    )
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "payload_too_large"


async def test_unknown_job_is_404(client):
    for path in ("/v1/reviews/nope", "/v1/reviews/nope/stream"):
        resp = await client.get(path, headers=AUTH)
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"


async def test_unknown_route_uses_the_error_envelope(client):
    resp = await client.get("/no/such/route")
    assert resp.status_code == 404
    assert set(resp.json()["error"]) == {"code", "message"}


async def test_unknown_body_fields_are_ignored(client):
    job_id = await submit(client, SIMPLE)
    resp = await client.post(
        "/v1/reviews",
        json={"diff": SIMPLE, "surprise": {"nested": 1}, "options": {"provider": "mock"}},
        headers=AUTH,
    )
    assert resp.status_code == 202
    assert job_id  # first submission was fine too


async def test_submit_returns_queued_envelope(client):
    resp = await client.post("/v1/reviews", json={"diff": SIMPLE}, headers=AUTH)
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "queued"
    assert isinstance(body["jobId"], str) and body["jobId"]


# --- startup safety -----------------------------------------------------


def test_unset_auth_token_refuses_to_start(monkeypatch):
    """A missing AUTH_TOKEN must crash the process, not silently serve an open
    API on a default that is published in this repo."""
    import importlib

    import app.config

    monkeypatch.delenv("AUTH_TOKEN", raising=False)
    # .env would otherwise repopulate the variable we just removed.
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)

    try:
        with pytest.raises(RuntimeError, match="AUTH_TOKEN"):
            importlib.reload(app.config)
    finally:
        monkeypatch.setenv("AUTH_TOKEN", "test-token")
        monkeypatch.undo()
        importlib.reload(app.config)


def test_blank_auth_token_is_treated_as_unset(monkeypatch):
    import importlib

    import app.config

    monkeypatch.setenv("AUTH_TOKEN", "   ")
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)

    try:
        with pytest.raises(RuntimeError, match="AUTH_TOKEN"):
            importlib.reload(app.config)
    finally:
        monkeypatch.setenv("AUTH_TOKEN", "test-token")
        monkeypatch.undo()
        importlib.reload(app.config)


# --- service index ------------------------------------------------------


async def test_root_lists_the_contract_routes(client):
    """`/` is a human convenience: opening the base URL in a browser should
    explain the service rather than look broken."""
    resp = await client.get("/")
    assert resp.status_code == 200

    body = resp.json()
    assert body["service"]
    assert body["version"] == VERSION

    listed = {(r["method"], r["path"]) for r in body["routes"]}
    assert listed == {
        ("GET", "/health"),
        ("GET", "/spec"),
        ("POST", "/v1/reviews"),
        ("GET", "/v1/reviews/{jobId}"),
        ("GET", "/v1/reviews/{jobId}/stream"),
    }
    # Each entry says whether a bearer token is needed.
    assert all(isinstance(r["auth"], bool) for r in body["routes"])
    assert {r["path"] for r in body["routes"] if r["auth"]} == {
        "/v1/reviews",
        "/v1/reviews/{jobId}",
        "/v1/reviews/{jobId}/stream",
    }


async def test_root_is_public(client):
    assert (await client.get("/")).status_code == 200


async def test_root_does_not_soften_unknown_routes(client):
    """Adding an index must not turn 404s into 200s elsewhere."""
    for path in ("/nope", "/health/extra"):
        resp = await client.get(path)
        assert resp.status_code == 404, path
        assert resp.json()["error"]["code"] == "not_found"

    # Under /v1 the auth gate answers first, so the 404 is only visible to a
    # caller who authenticated.
    for path in ("/v1", "/v1/"):
        assert (await client.get(path)).status_code == 401, path
        resp = await client.get(path, headers=AUTH)
        assert resp.status_code == 404, path
        assert resp.json()["error"]["code"] == "not_found"


# The complete code list published in the task. Nothing the service emits --
# in an error envelope or in a failed job body -- may fall outside it.
PUBLISHED_CODES = {
    "unauthorized",
    "payload_too_large",
    "invalid_json",
    "invalid_diff",
    "idempotency_conflict",
    "not_found",
    "rate_limited",
    "internal",
}


def test_no_source_site_invents_an_error_code():
    """Static guard over every code literal in `app/`.

    A failed job carries its error inside a 200 body rather than the error
    envelope, so nothing at runtime forces those codes to agree with the
    published list -- which is exactly how a `provider_error` once got in.
    This reads the call sites instead of waiting for a probe to hit one.
    """
    import ast
    import pathlib

    found: dict[str, str] = {}  # code -> where it came from
    for path in sorted(pathlib.Path("app").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            # ApiError(status, code, message) and job.fail(code, message).
            index = 1 if name == "ApiError" else 0 if name == "fail" else None
            if index is None or len(node.args) <= index:
                continue
            arg = node.args[index]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                found.setdefault(arg.value, f"{path}:{arg.lineno}")

    assert found, "found no error-code call sites -- the scan is broken, not the code"
    unpublished = {code: site for code, site in found.items() if code not in PUBLISHED_CODES}
    assert not unpublished, f"error codes outside the published taxonomy: {unpublished}"


@pytest.mark.parametrize("body", [b"[1, 2, 3]", b'"a string"', b"42", b"null"])
async def test_valid_json_that_is_not_an_object_is_400(client, body):
    """Parseable JSON of the wrong shape is a body problem, not a diff problem:
    there is no `diff` field to complain about."""
    resp = await client.post(
        "/v1/reviews", content=body, headers={**AUTH, "Content-Type": "application/json"}
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_json"
