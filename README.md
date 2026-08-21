# AI Diff Review Service

An HTTP service that accepts a unified diff, reviews it asynchronously through a
pluggable provider, and serves structured findings by polling or Server-Sent
Events.

Python 3.11+ / FastAPI. No database — see
[docs/adr/0002-in-memory-state.md](docs/adr/0002-in-memory-state.md).

## Running it

```bash
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt      # Windows: .venv\Scripts\pip
cp .env.example .env                               # set AUTH_TOKEN, GEMINI_API_KEY

.venv/bin/uvicorn app.main:app --port 8000
```

Or with Docker:

```bash
docker build -t diff-review .
docker run -p 8000:8000 -e AUTH_TOKEN=my-token -e GEMINI_API_KEY=... diff-review
```

Run with **one worker only**. Jobs, the result cache, idempotency keys and SSE
event logs are in-process state; a second worker would 404 on another worker's
jobs.

## Configuration

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `AUTH_TOKEN` | **yes** | none — startup fails | Bearer token for every `/v1/*` route |
| `GEMINI_API_KEY` | for `llm` | empty | Google Gemini key; without it `llm` jobs fail cleanly |
| `LLM_MODEL` | no | `gemini-3.5-flash-lite` | Model id for the `llm` provider |
| `LLM_TIMEOUT_SECONDS` | no | `20` | Per-request model timeout |
| `PORT` | no | `8000` | Listen port |

There is deliberately no default for `AUTH_TOKEN`: if it is unset the process
refuses to start. A deploy that forgets it should fail loudly in the logs rather
than serve an unprotected API that looks perfectly healthy.

`config.py` calls `load_dotenv()` at import, so a local `.env` is read
automatically. Real environment variables win over `.env`, which is how a
container's injected secrets stay authoritative. `.env` is gitignored; never
commit a key.

Model access lives entirely on the server. Callers send only the bearer token
and never an LLM key. `gemini-3.5-flash-lite` is the default because it answers
a single-chunk review in under two seconds, comfortably inside the 30-second
job budget; the larger `gemini-3.6-flash` took 4-22s and returned 503s under
load.

## Endpoints

| Route | Auth | Purpose |
|---|---|---|
| `GET /` | public | Service index listing the routes below (convenience, not part of the contract) |
| `GET /health` | public | `{status, version, uptimeSeconds}` |
| `GET /spec` | public | Declared limits, which match actual behaviour |
| `POST /v1/reviews` | bearer | Submit a diff, returns `202 {jobId, status:"queued"}` |
| `GET /v1/reviews/{jobId}` | bearer | Status, findings when done, usage |
| `GET /v1/reviews/{jobId}/stream` | bearer | SSE: `status`, `finding`, `done` |

```bash
curl -sX POST localhost:8000/v1/reviews \
  -H "Authorization: Bearer my-token" \
  -H "Idempotency-Key: demo-1" \
  -d '{"diff":"--- a/x.js\n+++ b/x.js\n@@ -0,0 +1,1 @@\n+eval(input);\n"}'
```

`options.provider` is `mock` (default) or `llm`; `options.maxFindings` defaults
to 100 and truncates the ordered list.

## Behaviour worth knowing

- **Ordering** is always `path`, then `line`, then `ruleId`, deduplicated by
  `id` — in responses and in streams alike. Files scan in path order, so each
  path's findings stream as soon as their ordered position is final.
- **Chunking**: diffs over 64 KiB split on file boundaries only. A chunked scan
  returns exactly what an unchunked scan would; `usage.chunks` reports the count.
- **Caching**: a byte-identical `{diff, provider, maxFindings}` returns a *new*
  jobId whose result reports `cacheHit: true` and reuses the earlier findings. A
  duplicate submitted while the first is still running waits for it rather than
  rescanning.
- **Idempotency**: `Idempotency-Key` with a byte-identical body returns the
  *same* jobId; a different body under the same key is `409`.
- **Retention**: jobs, cache entries, idempotency keys and SSE logs live for the
  process lifetime; the API declares no expiry.
- **Rate limiting** applies to `POST /v1/reviews` only, as a 30-token bucket
  refilling at 30/minute. GETs are never limited, and an idempotent replay is
  resolved before the limiter so an empty bucket cannot break the "same key →
  same jobId" guarantee.
- **Auth** is gated on the whole `/v1` prefix ahead of routing, so an unknown
  path or a disallowed method under `/v1` is `401` before it is anything else.
  405 collapses into 404 so every status maps onto a published error code.
- **Concurrency**: four jobs process at once; further jobs stay `queued`.

## Tests

```bash
.venv/bin/python -m pytest              # 192 tests, no network needed
.venv/bin/python -m pytest --no-cov     # same, without the coverage pass
.venv/bin/python scripts/smoke.py http://localhost:8000 my-token
```

`scripts/smoke.py` runs the same checks against a *running* service over real
HTTP; run it against the deployed URL before relying on it.

Coverage is on by default (`addopts = --cov` in `pytest.ini`) so a local run
gates exactly as CI does. Branch coverage is **99.8%**, and `.coveragerc` fails
the run below 95%. The one uncovered line is
[app/jobs.py:218](app/jobs.py#L218) — see *Known dead code* below.

## CI/CD

`.github/workflows/ci.yml` runs on every push to `main` and every pull request:

- **tests** on Python 3.11 and 3.12 — the floor the README declares and the
  version the Dockerfile ships, because a green suite on one says nothing about
  the other. Coverage XML is uploaded as an artifact.
- **docker image** — builds the Dockerfile Railway builds from, boots the
  container, and checks `/health`, `/spec` and a 401 on `/v1`. A build failure
  here is a deploy failure there.
- **startup-safety** — asserts the container *exits non-zero* with no
  `AUTH_TOKEN`. A unit test covers the `RuntimeError`; this covers the
  behaviour a deploy actually depends on.

Deployment is Railway's own GitHub integration, so `main` deploys itself. CI is
therefore a gate on what reaches `main`, not the thing that ships it.

`.github/workflows/smoke.yml` closes that loop: it runs `scripts/smoke.py`
against the live URL, on demand (`workflow_dispatch`, with the base URL as an
input) and daily at 07:00 UTC. It needs a repository secret
`SMOKE_AUTH_TOKEN` holding the bearer token — the token is never committed,
since this repository is public.

## Known dead code

[app/jobs.py:218](app/jobs.py#L218) — the `seen` set in `_run_scan` deduplicates
findings across flushes, but a path's findings are flushed exactly once: files
are sorted by path, so all sections of one path are contiguous, and a path is
only flushed when the next chunk starts at a later path. `normalize()` already
removes duplicates within that single flush, so this branch cannot be reached.
It is left in place rather than removed because it is the guard that would
matter if the flush rule ever changed.
