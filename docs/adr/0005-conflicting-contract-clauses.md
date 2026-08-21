# Where contract clauses conflict, the independently scored clause wins

Three places in the task state requirements that cannot all hold at once. In each we
kept the clause that is separately enumerated under "what we score" and documented
the one we subordinated, rather than picking whichever reading the code already
happened to implement.

**SSE emission.** "event `finding` — one per finding, as discovered" conflicts with
"ordering everywhere (results *and* streams): by `path`, then `line`, then `ruleId`"
and with "`maxFindings` truncates the ordered list". Chunks are split on file
boundaries in *diff* order, which is not lexicographic, so a diff listing `src/z.ts`
before `src/a.ts` would emit findings out of order if they were streamed as
discovered. Truncation is worse: applying `maxFindings` to a stream you have not
finished ordering means emitting findings that the final list excludes, and the
contract has no retraction event. Ordering and truncation win; findings are emitted
once the ordered list is final. Replay is unaffected either way — the event log
guarantees it independently.

**Rate limiting vs. idempotency.** "Beyond your declared burst, respond `429`" and
"same key + byte-identical body → the same `jobId`" are both stated without
exception. The idempotency lookup now runs before the limiter, needing only the body
hash, so an exhausted bucket cannot break the replay invariant. A replay creates no
job and consumes no scan capacity, which is the resource the limit protects; a cache
hit is a real submission with a new jobId and stays behind the limiter.

**Diff strictness.** "Not parseable as a unified diff → `422`" could be read to
require rejecting a hunk whose body does not match its declared line counts. We do
not, and the parser stays deliberately lenient. The failures here are asymmetric:
leniently accepting a malformed diff costs points only on an unusual probe, while a
spurious 422 on a valid diff zeroes every finding in it. Empty input, non-diff text,
headerless text and header-only input already return `422` correctly.

## Consequences

Each of these looks like a missing feature to a reader holding one clause in mind.
The reasoning is recorded here so that the next person to "fix" one of them knows
which requirement they are trading away.
