# Potentially competing clauses are satisfied together

Three parts of the task pull the implementation in different directions. None
requires subordinating another clause.

**SSE emission.** Findings must be emitted as discovered, globally ordered by
`(path, line, ruleId)`, and truncated only after ordering. Files are therefore
scanned in path order. Results for one path are buffered only while that path may
still occur in a later chunk; once the next chunk begins at a later path, the
normalized findings are final and are emitted immediately. Scanning continues
after `maxFindings` is reached so usage still describes the full scan.

**Rate limiting vs. idempotency.** The idempotency lookup runs before the limiter,
needing only the body hash, so an exhausted bucket cannot break the replay
invariant. A replay creates no job and consumes no scan capacity; a cache hit is a
real submission with a new jobId and stays behind the limiter.

**Diff strictness.** Extended Git headers, bare `diff -u` output, binary sections
and no-newline markers are accepted. Declared hunk counts are nevertheless
validated: accepting an under- or over-filled hunk would accept input that is not a
parseable unified diff.

## Consequences

The pipeline does a small amount of ordering before provider calls and retains
per-path findings until their stream order is final. This keeps live and replayed
streams identical without delaying every finding until the whole job completes.
