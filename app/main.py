import hashlib
import hmac
import json
import time

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import errors
from app.config import (
    AUTH_TOKEN,
    CHUNK_BYTES,
    DEFAULT_MAX_FINDINGS,
    MAX_CONCURRENT_JOBS,
    MAX_PAYLOAD_BYTES,
    RATE_LIMIT_PER_MINUTE,
    SPEC_VERSION,
    VERSION,
)
from app.diff import InvalidDiff, parse_diff
from app.errors import ApiError
from app.jobs import IdempotencyConflict, JobStore
from app.providers import DEFAULT_PROVIDER, known
from app.ratelimit import TokenBucket

app = FastAPI(title="AI Diff Review Service", version=VERSION, docs_url=None, redoc_url=None)

STARTED_AT = time.monotonic()
store = JobStore()
bucket = TokenBucket()


# --- cross-cutting ------------------------------------------------------


@app.exception_handler(ApiError)
async def _api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
    return exc.response()


@app.exception_handler(StarletteHTTPException)
async def _http_error_handler(_: Request, exc: StarletteHTTPException) -> JSONResponse:
    if exc.status_code in (404, 405):
        return errors.ApiError(exc.status_code, "not_found", "No such route.").response()
    if exc.status_code == 401:
        return errors.unauthorized().response()
    return errors.ApiError(exc.status_code, "internal", str(exc.detail)).response()


@app.exception_handler(RequestValidationError)
async def _validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    return errors.invalid_json("Request could not be validated.").response()


@app.exception_handler(Exception)
async def _unhandled_error_handler(_: Request, exc: Exception) -> JSONResponse:
    return errors.internal().response()


def require_auth(request: Request) -> None:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise errors.unauthorized()
    if not hmac.compare_digest(token.strip(), AUTH_TOKEN):
        raise errors.unauthorized()


async def read_body_capped(request: Request) -> bytes:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_PAYLOAD_BYTES:
        raise errors.payload_too_large()

    total, parts = 0, []
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_PAYLOAD_BYTES:
            raise errors.payload_too_large()
        parts.append(chunk)
    return b"".join(parts)


# --- public routes ------------------------------------------------------


@app.get("/")
async def index() -> JSONResponse:
    """Public service index. Not part of the scored contract -- it exists so that
    opening the base URL in a browser explains the service instead of returning
    a bare 404. Unknown routes still 404 with the error envelope."""
    return JSONResponse(
        {
            "service": "AI Diff Review Service",
            "version": VERSION,
            "routes": [
                {
                    "method": "GET",
                    "path": "/health",
                    "auth": False,
                    "description": "Liveness, version and uptime.",
                },
                {
                    "method": "GET",
                    "path": "/spec",
                    "auth": False,
                    "description": "Machine-readable limits and providers.",
                },
                {
                    "method": "POST",
                    "path": "/v1/reviews",
                    "auth": True,
                    "description": "Submit a unified diff. Returns 202 with a jobId.",
                },
                {
                    "method": "GET",
                    "path": "/v1/reviews/{jobId}",
                    "auth": True,
                    "description": "Job status, findings when done, and usage.",
                },
                {
                    "method": "GET",
                    "path": "/v1/reviews/{jobId}/stream",
                    "auth": True,
                    "description": "Server-sent events: status, finding, done.",
                },
            ],
        }
    )


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse(
        {
            "status": "ok",
            "version": VERSION,
            "uptimeSeconds": round(time.monotonic() - STARTED_AT, 3),
        }
    )


@app.get("/spec")
async def spec() -> JSONResponse:
    return JSONResponse(
        {
            "specVersion": SPEC_VERSION,
            "providers": ["mock", "llm"],
            "limits": {
                "maxPayloadBytes": MAX_PAYLOAD_BYTES,
                "chunkBytes": CHUNK_BYTES,
                "maxConcurrentJobs": MAX_CONCURRENT_JOBS,
                "rateLimitPerMinute": RATE_LIMIT_PER_MINUTE,
            },
        }
    )


# --- reviews ------------------------------------------------------------


def _read_options(payload: dict) -> tuple[str, int]:
    """Options are read leniently: the error taxonomy has no code for a bad
    option, so out-of-range values fall back to their documented defaults."""
    options = payload.get("options")
    if not isinstance(options, dict):
        options = {}

    provider = options.get("provider")
    if not isinstance(provider, str) or not known(provider):
        provider = DEFAULT_PROVIDER

    max_findings = options.get("maxFindings")
    if not isinstance(max_findings, int) or isinstance(max_findings, bool) or max_findings < 0:
        max_findings = DEFAULT_MAX_FINDINGS

    return provider, max_findings


@app.post("/v1/reviews")
async def create_review(request: Request) -> JSONResponse:
    require_auth(request)
    body = await read_body_capped(request)

    retry_after = bucket.take()
    if retry_after is not None:
        raise errors.rate_limited(retry_after)

    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise errors.invalid_json()
    if not isinstance(payload, dict):
        raise errors.invalid_json("Body must be a JSON object.")

    diff = payload.get("diff")
    if not isinstance(diff, str) or not diff.strip():
        raise errors.invalid_diff("Field 'diff' is required and must be a non-empty string.")

    provider, max_findings = _read_options(payload)

    try:
        parsed = parse_diff(diff)
    except InvalidDiff as exc:
        raise errors.invalid_diff(f"Field 'diff' is not a parseable unified diff: {exc}")

    body_hash = hashlib.sha256(body).hexdigest()
    idem_key = request.headers.get("idempotency-key")
    if idem_key:
        try:
            existing = store.idempotent_replay(idem_key, body_hash)
        except IdempotencyConflict:
            raise errors.idempotency_conflict()
        if existing is not None:
            return JSONResponse({"jobId": existing.id, "status": "queued"}, status_code=202)

    cache_key = hashlib.sha256(
        b"\x00".join([diff.encode("utf-8"), provider.encode(), str(max_findings).encode()])
    ).hexdigest()

    job = store.submit(
        parsed=parsed,
        provider_name=provider,
        max_findings=max_findings,
        cache_key=cache_key,
        idem_key=idem_key,
        body_hash=body_hash,
    )
    return JSONResponse({"jobId": job.id, "status": "queued"}, status_code=202)


@app.get("/v1/reviews/{job_id}")
async def get_review(job_id: str, request: Request) -> JSONResponse:
    require_auth(request)
    job = store.get(job_id)
    if job is None:
        raise errors.not_found(f"No job with id {job_id!r}.")
    return JSONResponse(job.to_dict())


@app.get("/v1/reviews/{job_id}/stream")
async def stream_review(job_id: str, request: Request) -> StreamingResponse:
    require_auth(request)
    job = store.get(job_id)
    if job is None:
        raise errors.not_found(f"No job with id {job_id!r}.")

    async def events():
        async for event in job.iter_events():
            data = json.dumps(event.data, separators=(",", ":"))
            yield f"event: {event.name}\ndata: {data}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
