"""Public endpoints, auth on every /v1 route, and the error taxonomy."""

import json

import pytest

from app.config import MAX_PAYLOAD_BYTES
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
