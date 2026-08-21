# Bird's-Eye Repository Map
_Generated 2026-08-21_

## System Purpose

An **AI Diff Review Service**: an HTTP API that accepts a unified diff, scans it
asynchronously through a pluggable **provider**, and serves structured **findings**
by polling or Server-Sent Events. Built as a take-home exercise
([CANDIDATE-TASK.md](CANDIDATE-TASK.md)) that is scored by calling a *running*
deployment against a fixed contract.

The problem domain is automated code review plumbing — not the review intelligence
itself. The interesting work is the pipeline around a scan: diff parsing, byte-exact
chunking, deterministic ordering and dedup, truncation, streaming with replay,
caching, idempotency, rate limiting, and concurrency capping. Two providers plug into
that pipeline: `mock` (a deterministic rules table, the thing actually scored) and
`llm` (Google Gemini, credentials server-side only).

## Top-Level Architecture

**One process. One worker. No database.** A single FastAPI/uvicorn ASGI app serving
six routes. There are no separate services, queues, or background daemons — async
work is `asyncio.Task`s inside the same event loop, gated by a semaphore.

All state — jobs, result cache, idempotency keys, SSE event logs — lives in
in-process dicts ([ADR-0002](docs/adr/0002-in-memory-state.md)). This is why the
[Dockerfile](Dockerfile) pins `--workers 1`: a second worker would 404 on another
worker's jobs and silently break caching and idempotency.

Communication is HTTP only: JSON request/response plus one `text/event-stream`
route. The only outbound call in the system is `llm` → Gemini
`generativelanguage.googleapis.com` over `httpx`.

## Major Areas

### Central

| Area | Why it exists |
|---|---|
| [app/main.py](app/main.py) | The HTTP edge. Routes, the ASGI auth gate, body-size cap, exception handlers, option parsing, cache-key derivation. Everything that turns bytes on a socket into a `JobStore.submit`. |
| [app/jobs.py](app/jobs.py) | The engine room. `Job` (status machine + append-only event log) and `JobStore` (caching, coalescing, idempotency, the concurrency semaphore, the scan loop that decides when a finding's stream order is final). |
| [app/diff.py](app/diff.py) | Unified-diff parser. Produces `ParsedDiff → FileDiff → Hunk → Line`, each `FileDiff` carrying its own path, new-file line numbers, raw text and exact byte count. |
| [app/providers/mock.py](app/providers/mock.py) | The scored rules table (MOCK-001…008, MOCK-INJ), plus a small JS string/comment masker so the non-literal rules can be read semantically. |

### Supporting

| Area | Why it exists |
|---|---|
| [app/findings.py](app/findings.py) | The `Finding` value object and `normalize()` — the *single* place ordering (`path, line, ruleId`) and dedup (by `id`) happen, so responses and streams cannot disagree. |
| [app/chunking.py](app/chunking.py) | 30 lines: pack `FileDiff`s into ≤64 KiB chunks, splitting on file boundaries only. |
| [app/providers/llm.py](app/providers/llm.py) | Gemini call plus strict validation of what comes back. |
| [app/providers/base.py](app/providers/base.py), [app/providers/__init__.py](app/providers/__init__.py) | The one-method `Provider` protocol, `ProviderError`, and the name→instance registry. |
| [app/errors.py](app/errors.py) | The whole error taxonomy as constructors. Every non-2xx in the service is an `ApiError`. |
| [app/config.py](app/config.py), [app/ratelimit.py](app/ratelimit.py) | Env/`.env` loading with a hard fail on missing `AUTH_TOKEN`; a 30-token bucket refilling at 30/min. |
| [tests/](tests/) | ~158 tests, no network. Split by concern: contract, edges, llm (fake transport), pipeline, rules. |
| [scripts/smoke.py](scripts/smoke.py) | The same checks over *real* HTTP against a running/deployed service. |
| [docs/adr/](docs/adr/) | Five ADRs recording the judgment calls a reader would otherwise mistake for bugs. |

`CONTEXT.md`, `PLAN.md`, `SUBMISSION.md`, `CANDIDATE-TASK.md` are project documents,
not code: the domain glossary, the build plan, the submission write-up, and the
original brief.

## Entry Points And Callers

- **Process start**: `uvicorn app.main:app`. Import of [app/config.py](app/config.py)
  runs `load_dotenv()` and raises `RuntimeError` if `AUTH_TOKEN` is unset — startup
  fails loudly rather than serving an open API.
- **ASGI stack**: `V1AuthGate` (raw ASGI, wraps the app) → Starlette router → handler.
  The gate is deliberately *not* `@app.middleware("http")`, which would wrap SSE in a
  `BaseHTTPMiddleware` streaming shim ([ADR-0004](docs/adr/0004-auth-is-a-prefix-gate.md)).

| Route | Auth | Handler |
|---|---|---|
| `GET /` | public | `index` — convenience route listing the contract routes; explicitly not part of the contract |
| `GET /health` | public | `health` |
| `GET /spec` | public | `spec` — declared limits, mirrored from `config.py` constants |
| `POST /v1/reviews` | bearer | `create_review` → `JobStore.submit` → `202 {jobId, status:"queued"}` |
| `GET /v1/reviews/{jobId}` | bearer | `get_review` → `Job.to_dict()` |
| `GET /v1/reviews/{jobId}/stream` | bearer | `stream_review` → `Job.iter_events()` as SSE |

Downstream: `LlmProvider.scan` → Gemini `:generateContent`. Nothing else leaves the
process.

## End-To-End Flow

### 1. Submit → scan → stream (the main path)

```
POST /v1/reviews
 └ V1AuthGate            bearer check on the /v1 prefix, before routing
 └ read_body_capped      streaming read, 413 past 1 MiB (declared or actual)
 └ sha256(body)          idempotency lookup FIRST — replay returns the SAME jobId
 └ TokenBucket.take()    429 + Retry-After when drained
 └ json.loads            400 invalid_json
 └ parse_diff(diff)      422 invalid_diff       str → ParsedDiff[FileDiff[Hunk[Line]]]
 └ sha256(diff|provider|maxFindings)  → cache key
 └ JobStore.submit
      ├ sort files by (path, first hunk new_start)
      ├ chunk_files       list[FileDiff] → list[list[FileDiff]]  (≤64 KiB, file-aligned)
      ├ cache key seen?   yes → _run_cached (no semaphore, just follows the source)
      └ no  → _run_scan
                async with semaphore (4)      ← a 5th job stays "queued" here
                for each chunk:
                  provider.scan(chunk)        → list[Finding]
                  buffer by path; flush paths that sort before the next chunk's path
                  normalize()  → dedup by id, order by (path, line, ruleId)
                  truncate at maxFindings, but KEEP SCANNING (usage describes the full scan)
                  job.add_findings()          → appends "finding" events to the log
                job.finish()                 → "status: done", then "done" {total, usage}
```

Data changes shape three times: **raw diff text → `ParsedDiff` (structured, with byte
spans and new-file line numbers) → `list[Finding]` → JSON dicts**. Chunking never
re-serializes, because each `FileDiff` already carries `raw_text`/`raw_bytes` sliced
from the original input.

### 2. Consuming a stream

`GET /v1/reviews/{jobId}/stream` is always *"replay the event log from index 0, then
follow it"*. `Job._notify()` swaps a fresh `asyncio.Event` on every append, so
followers wake without polling. A client attaching to an already-finished job
receives byte-identical events to one that watched live — replay is a property of the
design, not a special case.

A **cache hit** (`_run_cached`) iterates the *source job's* event log the same way and
mirrors its findings into its own log. That is what makes coalescing work: a duplicate
submitted while the original is still running simply follows it. It deliberately takes
no semaphore slot, which would otherwise deadlock against the scan it is waiting on.

## Non-Obvious Constraints

- **Single worker is mandatory.** Not a performance choice — correctness. See the
  comment in [Dockerfile](Dockerfile).
- **No expiry, anywhere.** Jobs, cache entries, idempotency keys and event logs are
  retained for the process lifetime, because the contract declares no expiry and
  eviction would break GET, SSE replay, caching and idempotency simultaneously. This
  is an unbounded-memory design *on purpose*.
- **Trigger form decides the reading** ([ADR-0003](docs/adr/0003-trigger-form-decides-the-reading.md)).
  The rules table states triggers at three levels of formality, and each rule is
  implemented at the level it was written. Consequence: **MOCK-005 looks like a bug
  and is not** — `x === null` fires, `x==null` does not, because the trigger is the
  literal fragment `== null`. Meanwhile MOCK-003 and MOCK-004 are prose triggers and
  are read semantically (string/comment masking, operand analysis of `+`).
- **Idempotency is resolved before the rate limiter**; a cache hit is not. A replay
  creates no job and consumes no capacity, so charging it a token would let an
  exhausted bucket break the "same key + identical body → same jobId" invariant
  ([ADR-0005](docs/adr/0005-conflicting-contract-clauses.md)).
- **405 collapses into 404** service-wide. The published taxonomy has `not_found` and
  no code for "wrong method", so every status the service emits maps onto a declared
  code ([ADR-0004](docs/adr/0004-auth-is-a-prefix-gate.md)).
- **Options are read leniently.** A bad `provider` or `maxFindings` silently falls
  back to the documented default, because the taxonomy has no error code for a bad
  option (`_read_options` in [app/main.py](app/main.py)).
- **`/spec` mirrors `config.py` constants** and a test asserts declared limits match
  actual behaviour. Changing a limit means changing one constant, not two places.
- **LLM output never reaches control flow.** Every returned finding must anchor to a
  `(path, line)` that is genuinely an added line in that chunk; `evidence` is then
  taken from *our* parse, never from the model. A hallucinated location is dropped.
  `MOCK-INJ` exists to prove injection text is *reported as content*, not obeyed.
- **Provider failures fail the job, never the process.** `ProviderError` → `failed`
  status with code `internal`; the cache entry is removed so a failure is never
  cached.
- **`mock` runs in a worker thread** (`asyncio.to_thread`) so CPU-bound regex work
  cannot stall SSE, health checks or the rate limiter.
- **Byte accounting is exact.** `usage.inputBytes` and chunk sizes are UTF-8 bytes,
  not characters; the parser tiles the whole input across file sections so nothing is
  unaccounted for (relevant for CRLF and multibyte content — both have tests).

## Glossary

- **Job** — one asynchronous review of one `{diff, options}` pair, opaque `jobId`,
  moving `queued → running → done | failed`.
- **Finding** — one rule violation on one added line; `id` is `ruleId:path:line`.
- **Provider** — the interchangeable scanning backend (`mock` | `llm`). Providers
  scan; the pipeline does everything else.
- **Idempotent replay** — repeated `Idempotency-Key` + byte-identical body → the
  *same* jobId, no new job. Beats caching.
- **Cache hit** — a *new* job answering a byte-identical `{diff, provider,
  maxFindings}` with an earlier job's findings, `cacheHit: true`, never rescanning.
- **Coalescing** — a cache hit against a still-running job: it awaits rather than
  scans.
- **Chunk** — a ≤64 KiB slice of a diff split only on file boundaries. One file's
  diff never spans two chunks.
- **Event log** — the append-only per-job list of SSE events (`status`, `finding`,
  `done`); streams replay it from index 0.
- **Trigger form** — the precision a rule's trigger is stated at (literal fragment /
  explicit regex / prose), which decides how literally it is implemented.

## Where To Start Reading

1. [CONTEXT.md](CONTEXT.md) — the domain vocabulary, one page. Read this first; the
   code uses these words precisely.
2. [app/main.py](app/main.py) — the whole HTTP surface and the submit-path ordering
   (auth → size → idempotency → limiter → JSON → diff).
3. [app/diff.py](app/diff.py) then [app/findings.py](app/findings.py) — the two data
   shapes everything else moves between.
4. [app/jobs.py](app/jobs.py) — the core. Read `Job` (event log) before `JobStore`
   (`_run_scan`'s per-path flush is the subtlest code in the repo).
5. [docs/adr/0003](docs/adr/0003-trigger-form-decides-the-reading.md) and
   [0005](docs/adr/0005-conflicting-contract-clauses.md) — before judging
   [app/providers/mock.py](app/providers/mock.py) or the streaming order.
6. [tests/test_pipeline.py](tests/test_pipeline.py) — the executable spec for
   chunking, streaming, caching, idempotency and concurrency. Test names read as
   sentences; skimming them is the fastest way to learn the contract.
