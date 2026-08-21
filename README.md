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
| `AUTH_TOKEN` | yes | `dev-token` | Bearer token for every `/v1/*` route |
| `GEMINI_API_KEY` | for `llm` | empty | Google Gemini key; without it `llm` jobs fail cleanly |
| `LLM_MODEL` | no | `gemini-3.5-flash-lite` | Model id for the `llm` provider |
| `LLM_TIMEOUT_SECONDS` | no | `20` | Per-request model timeout |
| `PORT` | no | `8000` | Listen port |

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
  `id` — in responses and in streams alike.
- **Chunking**: diffs over 64 KiB split on file boundaries only. A chunked scan
  returns exactly what an unchunked scan would; `usage.chunks` reports the count.
- **Caching**: a byte-identical `{diff, provider, maxFindings}` returns a *new*
  jobId whose result reports `cacheHit: true` and reuses the earlier findings. A
  duplicate submitted while the first is still running waits for it rather than
  rescanning.
- **Idempotency**: `Idempotency-Key` with a byte-identical body returns the
  *same* jobId; a different body under the same key is `409`.
- **Rate limiting** applies to `POST /v1/reviews` only, as a 30-token bucket
  refilling at 30/minute. GETs are never limited.
- **Concurrency**: four jobs process at once; further jobs stay `queued`.

## Tests

```bash
.venv/bin/python -m pytest              # 122 tests, no network needed
.venv/bin/python scripts/smoke.py http://localhost:8000 my-token
```

`scripts/smoke.py` runs the same checks against a *running* service over real
HTTP; run it against the deployed URL before relying on it.
