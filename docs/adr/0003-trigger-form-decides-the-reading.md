# Rule triggers are read at the formality they were written in

The mock-rules table does not describe its triggers at one level of precision: some
rows give a literal fragment (`eval(`, `== null`, `console.log(`, `TODO`), one gives
an explicit regex (MOCK-002), and two give a prose description (MOCK-003 "inside a
string concatenated with `+`", MOCK-004 "empty catch block (may span lines)"). We
implement each row at the level it was written: a literal fragment is matched as a
substring, the regex is used as given, and prose is read semantically. Applying one
uniform reading to all nine rules would mean overriding the task's own wording for
whichever rows did not fit it.

## Consequences

MOCK-005 is deliberately naive, and this is the row that looks like a bug: `x === null`
and `x !== null` both fire, because both literally contain `== null`, while `x==null`
does not fire, because it does not. This is not an oversight — the semantic reading
was implemented first and reverted. The single carve-out is a trailing word-boundary
guard so that `== nullable` stays silent, which no reading of the rule wants.

MOCK-003 and MOCK-004 are correspondingly *not* naive, because prose is not a
fragment to match. MOCK-003 requires the `+` to be an operand of the string literal
carrying the SQL keyword, so `"SELECT"; total = a + b` is not a finding. MOCK-004
requires `catch` to sit where a statement can begin, so `"catch {}"` inside a string
and `// catch {}` in a comment are not findings.

The rule that decides these cases is the task's own typography, which is a defensible
authority precisely because it is not ours.
