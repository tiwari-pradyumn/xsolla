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
    # 405 collapses into 404: the published taxonomy has one code for "no such
    # thing" and none for "wrong method", so emitting a 405 would mean a status
    # with no code to carry it.
    if exc.status_code in (404, 405):
        return errors.ApiError(404, "not_found", "No such route.").response()
    if exc.status_code == 401:
        return errors.unauthorized().response()
    return errors.ApiError(exc.status_code, "internal", str(exc.detail)).response()


@app.exception_handler(RequestValidationError)
async def _validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    return errors.invalid_json("Request could not be validated.").response()


@app.exception_handler(Exception)
async def _unhandled_error_handler(_: Request, exc: Exception) -> JSONResponse:
    return errors.internal().response()


def _bearer_ok(header: str) -> bool:
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return False
    return hmac.compare_digest(token.strip(), AUTH_TOKEN)


class V1AuthGate:
    """Authenticate every `/v1/*` request before routing.

    The contract puts auth on the route *prefix*, not on the handlers: an
    unknown path and a wrong method under `/v1` are both unauthorized before
    they are anything else. Checking inside handlers cannot express that,
    because Starlette resolves 404s and 405s before a handler ever runs.

    Written as raw ASGI rather than `@app.middleware("http")` on purpose.
    That decorator builds a `BaseHTTPMiddleware`, which re-wraps every response
    in its own streaming shim; SSE is a scored path and there is no reason to
    put a shim in it. This passes authorized traffic through untouched.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            path = scope["path"]
            if path == "/v1" or path.startswith("/v1/"):
                header = ""
                for key, value in scope["headers"]:
                    if key == b"authorization":
                        header = value.decode("latin-1")
                        break
                if not _bearer_ok(header):
                    await errors.unauthorized().response()(scope, receive, send)
                    return
        await self.app(scope, receive, send)


app.add_middleware(V1AuthGate)


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
    body = await read_body_capped(request)

    # Idempotency is resolved before the rate limiter, and needs nothing but the
    # body hash to do it. A replay creates no job and consumes no scan capacity,
    # so charging it a token would let an exhausted bucket break the contract's
    # "same key + identical body -> same jobId" invariant. A cache hit is a real
    # submission with a new jobId, so it stays behind the limiter.
    body_hash = hashlib.sha256(body).hexdigest()
    idem_key = request.headers.get("idempotency-key")
    if idem_key:
        try:
            existing = store.idempotent_replay(idem_key, body_hash)
        except IdempotencyConflict:
            raise errors.idempotency_conflict()
        if existing is not None:
            return JSONResponse({"jobId": existing.id, "status": "queued"}, status_code=202)

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
async def get_review(job_id: str) -> JSONResponse:
    job = store.get(job_id)
    if job is None:
        raise errors.not_found(f"No job with id {job_id!r}.")
    return JSONResponse(job.to_dict())


@app.get("/v1/reviews/{job_id}/stream")
async def stream_review(job_id: str) -> StreamingResponse:
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
