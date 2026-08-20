"""The llm provider: schema validation of model output, and graceful failure."""

import httpx
import pytest

from app.diff import parse_diff
from app.providers import llm as llm_module
from app.providers.base import ProviderError
from tests.conftest import AUTH, file_section, submit, wait_done

DIFF = file_section("src/a.ts", ["eval(x);", "console.log(1);"])
FILES = parse_diff(DIFF).files


def response_with(findings: list[dict]) -> dict:
    import json

    return {"candidates": [{"content": {"parts": [{"text": json.dumps({"findings": findings})}]}}]}


def index():
    return llm_module._added_index(FILES)


def test_valid_model_output_is_accepted():
    payload = response_with(
        [
            {
                "path": "src/a.ts",
                "line": 1,
                "severity": "critical",
                "category": "security",
                "title": "eval on user input",
            }
        ]
    )
    findings = llm_module._parse_response(payload, index())
    assert len(findings) == 1
    assert findings[0].rule_id == "LLM-SECURITY"
    assert findings[0].evidence == "eval(x);"  # taken from our parse, not the model


@pytest.mark.parametrize(
    "item",
    [
        {"path": "src/nope.ts", "line": 1, "severity": "high", "category": "security", "title": "x"},
        {"path": "src/a.ts", "line": 99, "severity": "high", "category": "security", "title": "x"},
        {"path": "src/a.ts", "line": 1, "severity": "urgent", "category": "security", "title": "x"},
        {"path": "src/a.ts", "line": 1, "severity": "high", "category": "vibes", "title": "x"},
        {"path": "src/a.ts", "line": 1, "severity": "high", "category": "security", "title": ""},
        {"path": "src/a.ts", "line": "1", "severity": "high", "category": "security", "title": "x"},
        {"line": 1, "severity": "high", "category": "security", "title": "x"},
        "not even an object",
    ],
)
def test_unanchored_or_malformed_model_output_is_dropped(item):
    assert llm_module._parse_response(response_with([item]), index()) == []


def test_non_json_model_output_fails_the_provider():
    payload = {"candidates": [{"content": {"parts": [{"text": "sorry, I cannot"}]}}]}
    with pytest.raises(ProviderError):
        llm_module._parse_response(payload, index())


def test_unexpected_response_shape_fails_the_provider():
    with pytest.raises(ProviderError):
        llm_module._parse_response({"error": {"code": 429}}, index())


def test_diff_is_delivered_as_fenced_data():
    prompt = llm_module._build_prompt(FILES)
    assert "<diff>" in prompt and "</diff>" in prompt
    assert "untrusted DATA" in llm_module.SYSTEM_INSTRUCTION


# --- graceful degradation through the API -------------------------------


async def test_missing_credentials_fail_the_job_not_the_service(client, monkeypatch):
    monkeypatch.setattr(llm_module, "GEMINI_API_KEY", "")

    body = await wait_done(client, await submit(client, DIFF, provider="llm"))
    assert body["status"] == "failed"
    assert body["error"]["code"] == "provider_error"
    assert "GEMINI_API_KEY" in body["error"]["message"]

    assert (await client.get("/health")).status_code == 200
    mock_job = await wait_done(client, await submit(client, DIFF))
    assert mock_job["status"] == "done"


class FakeClient:
    """Stands in for the provider's own HTTP client only, so the test client
    talking to the app is untouched."""

    def __init__(self, *, response=None, error=None):
        self._response = response
        self._error = error

    def __call__(self, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, *args, **kwargs):
        if self._error:
            raise self._error
        return self._response


async def test_unreachable_model_fails_the_job(client, monkeypatch):
    monkeypatch.setattr(llm_module, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(
        llm_module, "AsyncClient", FakeClient(error=httpx.ConnectError("no route to host"))
    )

    body = await wait_done(client, await submit(client, DIFF, provider="llm"))
    assert body["status"] == "failed"
    assert body["error"]["code"] == "provider_error"
    assert "Could not reach the model" in body["error"]["message"]


async def test_model_http_error_fails_the_job(client, monkeypatch):
    monkeypatch.setattr(llm_module, "GEMINI_API_KEY", "test-key")

    monkeypatch.setattr(
        llm_module,
        "AsyncClient",
        FakeClient(
            response=httpx.Response(
                429, text="quota exceeded", request=httpx.Request("POST", "http://x")
            )
        ),
    )

    body = await wait_done(client, await submit(client, DIFF, provider="llm"))
    assert body["status"] == "failed"
    assert "429" in body["error"]["message"]


async def test_a_failed_job_is_not_cached(client, monkeypatch):
    monkeypatch.setattr(llm_module, "GEMINI_API_KEY", "")

    first = await wait_done(client, await submit(client, DIFF, provider="llm"))
    second = await wait_done(client, await submit(client, DIFF, provider="llm"))

    assert first["status"] == second["status"] == "failed"
    assert second["usage"]["cacheHit"] is False


async def test_successful_llm_job_flows_through_the_pipeline(client, monkeypatch):
    monkeypatch.setattr(llm_module, "GEMINI_API_KEY", "test-key")

    payload = response_with(
        [
            {
                "path": "src/a.ts",
                "line": 2,
                "severity": "low",
                "category": "style",
                "title": "debug logging left in",
            },
            {
                "path": "src/a.ts",
                "line": 1,
                "severity": "critical",
                "category": "security",
                "title": "eval on user input",
            },
        ]
    )
    monkeypatch.setattr(
        llm_module,
        "AsyncClient",
        FakeClient(
            response=httpx.Response(200, json=payload, request=httpx.Request("POST", "http://x"))
        ),
    )

    body = await wait_done(client, await submit(client, DIFF, provider="llm"))
    assert body["status"] == "done"
    # Same ordering guarantee as the mock path.
    assert [(f["line"], f["ruleId"]) for f in body["findings"]] == [
        (1, "LLM-SECURITY"),
        (2, "LLM-STYLE"),
    ]
    assert body["findings"][0]["evidence"] == "eval(x);"


async def test_stream_of_a_failed_job_terminates(client, monkeypatch):
    from tests.conftest import parse_sse, read_stream

    monkeypatch.setattr(llm_module, "GEMINI_API_KEY", "")

    job_id = await submit(client, DIFF, provider="llm")
    await wait_done(client, job_id)

    events = parse_sse(await read_stream(client, job_id))
    names = [n for n, _ in events]
    assert "finding" not in names
    assert names[-1] == "done"
    assert '"status":"failed"' in "".join(d for _, d in events)
