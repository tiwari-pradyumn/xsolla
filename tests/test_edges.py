"""Contract edges that the main suites assume rather than verify."""

import json

from app.config import MAX_PAYLOAD_BYTES
from tests.conftest import (
    AUTH,
    file_section,
    parse_sse,
    read_stream,
    submit,
    wait_done,
)

BASE = file_section("src/a.ts", ["eval(x);"])


def body_of_exactly(size: int) -> bytes:
    """A valid submission whose encoded body is exactly `size` bytes.

    Padding is appended after the last hunk, where the parser ignores it, and
    every padding character encodes to one byte and needs no JSON escaping.
    """
    overhead = len(json.dumps({"diff": BASE}).encode())
    body = json.dumps({"diff": BASE + "x" * (size - overhead)}).encode()
    assert len(body) == size, (len(body), size)
    return body


async def test_payload_of_exactly_the_limit_is_accepted(client):
    resp = await client.post(
        "/v1/reviews",
        content=body_of_exactly(MAX_PAYLOAD_BYTES),
        headers={**AUTH, "Content-Type": "application/json"},
    )
    assert resp.status_code == 202, resp.text


async def test_payload_one_byte_over_the_limit_is_rejected(client):
    resp = await client.post(
        "/v1/reviews",
        content=body_of_exactly(MAX_PAYLOAD_BYTES + 1),
        headers={**AUTH, "Content-Type": "application/json"},
    )
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "payload_too_large"


# --- non-ASCII ----------------------------------------------------------

MULTIBYTE_LINE = 'console.log("héllo — 日本語 🎉");'


async def test_usage_counts_bytes_not_characters(client):
    diff = file_section("src/i18n.ts", ["const a = 1;", MULTIBYTE_LINE])
    encoded = len(diff.encode("utf-8"))
    assert encoded > len(diff), "fixture must actually be multibyte"

    body = await wait_done(client, await submit(client, diff))
    assert body["usage"]["inputBytes"] == encoded


async def test_multibyte_evidence_survives_verbatim(client):
    diff = file_section("src/i18n.ts", ["const a = 1;", MULTIBYTE_LINE])
    body = await wait_done(client, await submit(client, diff))

    finding = body["findings"][0]
    assert finding["ruleId"] == "MOCK-007"
    assert finding["evidence"] == MULTIBYTE_LINE
    assert finding["line"] == 2


# --- CRLF ---------------------------------------------------------------


async def test_windows_line_endings_parse_identically(client):
    """A diff produced on Windows must yield the same findings, with the line
    terminator excluded from evidence."""
    lines = ["const a = 1;", "eval(danger);", "// TODO: later"]
    crlf = file_section("src/win.ts", lines).replace("\n", "\r\n")

    body = await wait_done(client, await submit(client, crlf))
    assert [(f["ruleId"], f["line"]) for f in body["findings"]] == [
        ("MOCK-001", 2),
        ("MOCK-008", 3),
    ]
    assert all("\r" not in f["evidence"] for f in body["findings"])
    assert body["findings"][0]["evidence"] == "eval(danger);"


def test_crlf_bytes_are_counted_for_chunking():
    """\\r\\n costs two bytes; chunk sizing must reflect the wire form."""
    from app.diff import parse_diff

    lines = ["const a = 1;", "eval(x);"]
    lf = parse_diff(file_section("src/win.ts", lines))
    crlf = parse_diff(file_section("src/win.ts", lines).replace("\n", "\r\n"))

    assert crlf.input_bytes > lf.input_bytes
    assert crlf.files[0].raw_bytes > lf.files[0].raw_bytes


# --- maxFindings and the stream -----------------------------------------

FIVE = file_section("src/many.ts", [f"eval(x{i});" for i in range(5)])


async def test_stream_emits_only_the_truncated_findings(client):
    job_id = await submit(client, FIVE, maxFindings=2)
    body = await wait_done(client, job_id)
    assert len(body["findings"]) == 2

    events = parse_sse(await read_stream(client, job_id))
    streamed = [json.loads(data) for name, data in events if name == "finding"]

    assert streamed == body["findings"], "stream and response must agree"
    assert len(streamed) == 2


async def test_done_total_matches_the_findings_delivered(client):
    job_id = await submit(client, FIVE, maxFindings=2)
    body = await wait_done(client, job_id)

    events = parse_sse(await read_stream(client, job_id))
    done = json.loads(events[-1][1])

    assert done["total"] == len(body["findings"]) == 2
    assert done["usage"] == body["usage"]


async def test_max_findings_of_zero_yields_an_empty_but_complete_job(client):
    job_id = await submit(client, FIVE, maxFindings=0)
    body = await wait_done(client, job_id)
    assert body["status"] == "done"
    assert body["findings"] == []

    events = parse_sse(await read_stream(client, job_id))
    assert not [name for name, _ in events if name == "finding"]
    assert json.loads(events[-1][1])["total"] == 0
    # The scan still happened: usage describes the whole input.
    assert body["usage"]["inputBytes"] == len(FIVE.encode())
    assert body["usage"]["chunks"] == 1
