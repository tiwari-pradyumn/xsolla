# AI Diff Review Service

A single-instance HTTP service that accepts unified diffs, scans them asynchronously through a provider, and serves structured review findings.

## Language

**Job**:
One asynchronous review of one submitted `{diff, options}` pair, identified by an opaque jobId, moving through `queued → running → done | failed`.
_Avoid_: task, review request

**Finding**:
One rule violation on one added line, identified by `ruleId:path:line`.
_Avoid_: issue, result, violation

**Provider**:
The interchangeable scanning backend (`mock` or `llm`) behind the shared pipeline; providers scan, the pipeline does everything else.
_Avoid_: engine, backend, model

**Idempotent replay**:
The response to a repeated `Idempotency-Key` with a byte-identical body: the *same* jobId, no new job. Takes precedence over caching.
_Avoid_: cache hit, dedupe

**Cache hit**:
A *new* job answering a byte-identical `{diff, provider, maxFindings}` with the stored findings of an earlier job, reporting `cacheHit: true`, never rescanning.
_Avoid_: idempotent replay, memoization

**Coalescing**:
A cache hit against a job that is still running: the new job awaits the original's completion instead of scanning.

**Trigger form**:
The level of precision a rule's trigger is stated at — literal fragment, explicit regex, or prose description — which is what decides how literally that rule is read.
_Avoid_: rule syntax, matcher type

**Chunk**:
A ≤64 KiB slice of a diff split only on file boundaries; the unit a provider scans. A single file's diff never spans two chunks.

**Event log**:
The append-only per-job list of SSE events (`status`, `finding`, `done`); streams replay it from index 0, so live and after-the-fact consumers see identical sequences.
