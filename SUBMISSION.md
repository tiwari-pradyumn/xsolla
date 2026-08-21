# Submission

- **Base URL:** https://xsolla-production.up.railway.app
- **Bearer token:** sent privately with the submission message. It is deliberately
  not recorded here: this repository is public, and a token committed to it would
  make the service open to anyone who reads the file.
- **Repository:** https://github.com/tiwari-pradyumn/xsolla

Health check, no auth required:

```bash
curl https://xsolla-production.up.railway.app/health
curl https://xsolla-production.up.railway.app/spec
```

## Architecture

One FastAPI process, no database. A submission is validated at the HTTP edge
(auth → size → rate limit → JSON → diff), parsed into file sections, split into
≤64 KiB chunks on file boundaries, and handed to a worker task that acquires a
4-slot semaphore before scanning. Because every parsed file carries its own path
and new-file line numbers, chunk results merge with no positional fixup — which
is *why* a chunked scan is identical to an unchunked one, rather than something
patched up afterwards. Findings are deduplicated by `id` and ordered by
`(path, line, ruleId)` in one place, so responses and streams cannot disagree.

Each job owns an append-only event log. A stream is always "replay the log from
index 0, then follow it", so a client attaching to a finished job receives
byte-identical events to one that watched it live; replay is a property of the
design, not a special case. State lives in memory
([ADR-0002](docs/adr/0002-in-memory-state.md)); the service must run as a single
worker.

## Provider design

`Provider` is one method: `scan(files) -> findings`. Everything scored —
parsing, chunking, ordering, dedup, truncation, streaming, caching, concurrency
— lives in the pipeline, not the provider, so `mock` and `llm` are
interchangeable and the pipeline is testable without a model.

`mock` implements the rules table and is pure and deterministic. It runs in a
worker thread (`asyncio.to_thread`) so regex work never blocks the event loop,
keeping SSE, health and rate limiting responsive while scans run.

`llm` (Google Gemini, key in `GEMINI_API_KEY`, never sent by callers) posts the
chunk to the model and validates what comes back. Two properties are structural
rather than prompt-based:

1. The diff is delivered as fenced data with a system instruction stating it is
   untrusted content. Nothing the model returns reaches control flow.
2. Every returned finding must name a `(path, line)` that is genuinely an added
   line in that chunk. `evidence` is then taken from **our** parse, never from
   the model — so evidence is verbatim by construction and a hallucinated
   location is dropped rather than reported.

Any transport, quota, credential or parse failure raises `ProviderError`, which
fails that job with a clear message and evicts its cache entry so the failure is
not memoised. The process never dies; a `mock` job submitted immediately after a
model outage still succeeds (covered by a test).

## How the cross-cutting behaviours were verified

122 tests (`pytest`, no network) plus `scripts/smoke.py`, which re-runs the same
checks against a *running* service over real HTTP — SSE in particular behaves
differently through an in-process transport than through uvicorn, so the
in-process suite alone would not have been evidence. All 33 smoke checks pass
against a local uvicorn instance.

- **Chunking** — a generated 40-file diff is scanned whole and per-chunk, and
  the normalised results are asserted equal; separately, chunk sizes stay under
  the limit unless a single file exceeds it, file order is preserved, and no
  `id` appears twice.
- **SSE replay** — the stream of a finished job is read twice and compared as
  raw bytes; a live attach is compared against the later replay; a deliberately
  slowed provider proves a client attaching before any finding exists still
  receives the full sequence.
- **Caching** — a counting provider proves the second identical submission
  performs *zero* scans, and a slow provider proves a duplicate submitted while
  the first is still running coalesces onto it instead of rescanning.
- **Idempotency** — same key + same body returns the same jobId; a different
  body is 409; a replayed key beats the cache (the original job is returned, and
  it still reports `cacheHit: false`).
- **Concurrency** — an instrumented provider records peak concurrency of exactly
  4 while a 5th job is observed `queued`, and all five reach `done`.
- **Rate limiting** — a 45-request burst yields 429s with `Retry-After` and zero
  5xx; GETs remain unlimited with the bucket drained to zero.
- **Injection inertness** — injection phrasing is reported as `MOCK-INJ` while
  every other rule on the surrounding lines still fires unchanged.
- **Error taxonomy** — each code asserted on its own trigger, including that
  unknown routes and unauthenticated requests use the same envelope.

## Interpretation calls

The rules table leaves a few things open. Each of these is a decision, not an
oversight.

The governing one is **[ADR-0003](docs/adr/0003-trigger-form-decides-the-reading.md):
each trigger is implemented at the level of formality the table wrote it in.**
The table is not uniform — five rows give a literal fragment, one gives an
explicit regex, and two give a prose description — so a single uniform reading
would mean overriding the task's own wording for whichever rows did not fit it.
The task's typography decides, which is a defensible authority precisely because
it is not ours.

- **MOCK-005 is deliberately naive.** The table writes a literal fragment, so it
  is matched literally: `x === null` and `x !== null` both fire (both contain
  `== null`), and `x==null` does not (it does not). This is the row that looks
  most like a bug, and it is the one we debated hardest — the semantic reading,
  where the trigger names the *operators* and spacing is irrelevant, was
  implemented first and then reverted under ADR-0003. The single carve-out is a
  trailing word-boundary guard so `== nullable` stays silent, which no reading of
  the rule wants.
- **MOCK-003 is correspondingly not naive**, because prose is not a fragment to
  match. "Concatenated with `+`" is a claim about the operator's *operands*, so
  the `+` must touch the string literal carrying the keyword: `"SELECT"; total =
  a + b` is not a finding. Keywords match case-insensitively: SQL itself is
  case-insensitive, so `"select * from t" + id` is the same vulnerability as its
  uppercase twin. Challenged in review (the table writes the keywords in
  uppercase, and `"Delete user " + name` is a false positive under this reading)
  and deliberately kept: missing a real lowercase SQL injection is a worse
  failure for a security rule than flagging UI text.
- **MOCK-008 is case-sensitive.** `TODO`/`FIXME` are markers; lowercase `todo`
  appears in ordinary identifiers like `todoList`.
- **MOCK-004** treats a whitespace-only body as empty, detects blocks spanning
  lines by reconstructing the hunk's new-file text (so a block interleaved with
  unchanged lines is still found), and reports only when the `catch` line itself
  was added. `catch` must also sit where a statement can begin — after `}`, `;`,
  `{` or a line break — so `"catch {}"` inside a string, `// catch {}` in a
  comment and `p.catch(e => {})` are not findings.
- **A cache hit mints a new jobId.** The contract requires the first run to
  report `cacheHit: false` and the repeat to report `true`, so one jobId cannot
  serve both. An `Idempotency-Key` replay takes precedence and returns the
  original job.
- **Invalid `provider` / `maxFindings` values fall back to their defaults.** The
  error taxonomy has no code for a bad option, and reusing `invalid_diff` would
  be a lie about which field was wrong.
- **Failed jobs emit a `done` event** before closing, so every stream terminates
  the same way. `total` counts the findings actually emitted (post-truncation).
- Binary and rename-only file sections are parsed and contribute no findings.
- **Auth is a gate on the `/v1` prefix, not a check inside the handlers**
  ([ADR-0004](docs/adr/0004-auth-is-a-prefix-gate.md)). Starlette resolves
  unknown paths and disallowed methods before any handler runs, so a handler
  check cannot speak for `PUT /v1/reviews`. It runs as raw ASGI rather than
  `@app.middleware("http")`, which would wrap SSE in a `BaseHTTPMiddleware`
  streaming shim. **405 collapses into 404** throughout: the taxonomy has one
  code for "no such thing" and none for "wrong method", so every status the
  service emits maps onto a code the task published.

Three clauses in the contract contradict other clauses. Each is resolved in
favour of the requirement that is separately enumerated under "what we score",
and the reasoning is in [ADR-0005](docs/adr/0005-conflicting-contract-clauses.md):

- **Findings stream once the ordered list is final, not "as discovered".**
  Chunks split on file boundaries in *diff* order, which is not lexicographic, so
  as-discovered emission breaks the ordering guarantee on any diff whose files
  are not already sorted. Truncation settles it outright: `maxFindings` applies
  to the *ordered* list, and there is no retraction event for a finding already
  emitted. Replay is unaffected — the event log guarantees it independently.
- **Idempotency resolves before the rate limiter.** "Same key + identical body →
  same `jobId`" is stated without exception, so an exhausted bucket must not turn
  a replay into a `429`. A replay creates no job and consumes no scan capacity; a
  cache hit is a real submission with a new jobId and stays behind the limiter.
- **The diff parser stays lenient** and accepts a hunk whose body does not match
  its declared counts. The failure modes are asymmetric: leniency costs points
  only on an unusual probe, while a spurious `422` on a valid diff zeroes every
  finding in it. Empty, non-diff, headerless and header-only input already `422`.

- **Job retention is a memory bound before it is a policy.** A retained job holds
  its findings *and* its event log — measured at 6.3 KiB empty, 12 KiB typical,
  95 KiB for a 100-finding job — so the cap is set at 5,000 (~60 MB typical,
  ~475 MB worst case). An earlier 20,000 would have reached 1.8 GiB and risked an
  OOM, which loses every job rather than the oldest ones. The cache and
  idempotency indexes are deliberately *not* swept: ~900 bytes per submission, or
  ~78 MB across a full 48-hour window, and both self-heal when the job they point
  at is gone.

## AI tools used

Built with Claude Code (Claude Fable 5) throughout. The session began as a
structured design interview over the brief rather than a code request: the
questions that came back were about cache-hit jobId semantics, where "declared
burst" should live given `/spec` has no field for it, and the MOCK-004 ambiguity
— which surfaced the decisions above *before* any code existed. That interview
produced `PLAN.md`, `CONTEXT.md` (a glossary that keeps "cache hit" and
"idempotent replay" from being conflated) and the first two ADRs. Implementation was
then largely AI-written against that plan.

Being straight about the process: it was *not* test-first. The modules were
written in one pass and the tests followed, which is why they all passed
immediately — and a test that has never been seen failing has not shown it can
detect anything. A later pass audited that. One test was found to be inert:
it asserted `usage.chunks == len(chunk_files(...))`, comparing the service
against the very function under test, so it passed by construction. Replacing
`chunk_files` with `return [list(files)]` — disabling chunking entirely — left
it green. It now derives its expectations from the fixture's construction and
fails that mutation with `assert 4 <= 1`.

**An AI suggestion I rejected:** the recommended stack was Node + TypeScript,
argued from the mock rules being JS-flavoured and SSE being first-class there. I
rejected it and chose Python + FastAPI. The reasoning is recorded in
[ADR-0001](docs/adr/0001-python-fastapi.md): the language scanning a diff is
independent of the language *inside* the diff, so the JS-flavour argument was
close to irrelevant, while the real constraint — that I have to defend every
line of this in an interview — pointed the other way. The one genuine cost, that
the MOCK-002 regex is JS syntax needing a careful port, is pinned by tests.

## What I skipped, and what's next

**Deployment:** Railway, on its free trial tier, as a single always-on
container built from the repo's Dockerfile. The choice followed from
[ADR-0002](docs/adr/0002-in-memory-state.md): because state is in memory, the
host must never scale to zero — a cold start both loses jobs and threatens the
30-second budget. That ruled out Render's free tier (sleeps after 15 minutes,
~60s cold start) and, as of 2026, Fly.io and Koyeb no longer offer free tiers to
new accounts. Railway does not sleep, needs no card, and its 30-day trial credit
outlasts the scoring window comfortably.

All 33 smoke checks pass against the deployed URL, including a live `llm` job.

**The `llm` path is verified end to end** against a live Gemini key: a real
review returns correctly anchored findings in ~1.6s, and `scripts/smoke.py`
reports the job reaching `done`. Two failure modes were observed for real while
getting there and both degraded exactly as designed — `gemini-2.0-flash` had
been retired (HTTP 404) and `gemini-3.6-flash` returned 503 under load; each
failed one job with a clear message and left the service healthy. The default
model is `gemini-3.5-flash-lite`, chosen on measured latency: flash-lite answers
in ~1.6s against 4-22s for `gemini-3.6-flash`, which matters against the
30-second job budget.

Skipped deliberately:

- **Persistence.** In-memory only; a restart loses jobs and the cache. Correct
  for a 48-hour single-instance window, wrong for production, and the first
  thing I would change — SQLite behind the same `JobStore` interface.
- **Job eviction** beyond a 20,000-job cap. Fine for the scoring window; a real
  service wants TTLs.
- **Per-client rate limiting.** One global bucket, since the contract describes
  a single caller. Multi-tenant use needs a per-token bucket.
- **Parallel chunk scanning within a job.** Chunks are scanned sequentially,
  which is irrelevant for `mock` and for the 30-second budget (which applies to
  single-chunk diffs) but would matter for large `llm` reviews.

With more time: SQLite persistence, `Last-Event-ID` resumption so a dropped SSE
client can resume mid-stream instead of replaying, and a fuzz harness generating
random diffs to assert the chunked/unchunked equivalence continuously rather
than on one crafted example.
