"""The deterministic mock provider: the finding-rules table, implemented exactly.

Rules apply to added lines only. `line` is the line number in the new file.

Each trigger is implemented at the level of formality the task wrote it in
(ADR-0003): a literal fragment is matched as a substring, an explicit regex is
used as given, and a prose description is read semantically. That is why
MOCK-005 is deliberately naive while MOCK-003 and MOCK-004 are not.
"""

import asyncio
import re
from bisect import bisect_right

from app.diff import FileDiff, Hunk
from app.findings import Finding

RULES = {
    "MOCK-001": ("critical", "security", "eval usage"),
    "MOCK-002": ("critical", "security", "hardcoded credential"),
    "MOCK-003": ("high", "security", "SQL string concatenation"),
    "MOCK-004": ("high", "correctness", "swallowed exception"),
    "MOCK-005": ("medium", "correctness", "loose null comparison"),
    "MOCK-006": ("medium", "performance", "deep-clone via JSON"),
    "MOCK-007": ("low", "style", "console.log left in"),
    "MOCK-008": ("low", "style", "unresolved marker"),
    "MOCK-INJ": ("critical", "security", "prompt-injection content"),
}

CREDENTIAL_RE = re.compile(
    r"""(api[_-]?key|secret|token)\s*[:=]\s*['"][A-Za-z0-9_\-]{16,}['"]""", re.I
)

SQL_KEYWORD_RE = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE)\b", re.I)

# After strings and comments are masked, a whitespace-only body is empty. The
# catch binding is optional (JS allows `catch {}`), and `catch` must sit where a
# statement can start so a method named `catch` is not mistaken for a block.
# Group 1 is the `catch` itself, which is the position the finding is reported at.
EMPTY_CATCH_RE = re.compile(r"(?:^|[\n;{}])\s*(catch\s*(?:\([^)]*\))?\s*\{\s*\})")

INJECTION_PHRASES = (
    "ignore previous instructions",
    "disregard all prior",
    "you are now",
)


def _scan_source(text: str) -> tuple[list[tuple[int, int, int, int]], str, str]:
    """Return string spans plus same-length syntax and comment masks.

    The syntax mask replaces strings with `x` and comments with spaces, keeping
    newlines and offsets intact. The comment mask removes only comments, which
    lets the SQL rule inspect operators around a real string literal.
    """
    spans: list[tuple[int, int, int, int]] = []
    syntax = list(text)
    comments = list(text)
    i, n = 0, len(text)

    def mask(target: list[str], start: int, end: int, replacement: str) -> None:
        for pos in range(start, end):
            if target[pos] not in ("\r", "\n"):
                target[pos] = replacement

    while i < n:
        c = text[i]
        if c in ("'", '"', "`"):
            quote = c
            opening = i
            i += 1
            content_start = i
            while i < n:
                if text[i] == "\\" and i + 1 < n:
                    i += 2
                    continue
                if text[i] == quote:
                    break
                i += 1
            if i == n:  # unterminated data cannot be an operand
                mask(syntax, opening, n, "x")
                break
            spans.append((opening, content_start, i, i))
            mask(syntax, opening, i + 1, "x")
            i += 1
            continue

        if c == "/" and i + 1 < n and text[i + 1] == "/":
            start = i
            i += 2
            while i < n and text[i] not in ("\r", "\n"):
                i += 1
            mask(syntax, start, i, " ")
            mask(comments, start, i, " ")
            continue

        if c == "/" and i + 1 < n and text[i + 1] == "*":
            start = i
            i += 2
            while i + 1 < n and text[i : i + 2] != "*/":
                i += 1
            i = min(n, i + 2)
            mask(syntax, start, i, " ")
            mask(comments, start, i, " ")
            continue

        i += 1

    return spans, "".join(syntax), "".join(comments)


def _previous_nonspace(text: str, index: int) -> int | None:
    index -= 1
    while index >= 0 and text[index].isspace():
        index -= 1
    return index if index >= 0 else None


def _next_nonspace(text: str, index: int) -> int | None:
    while index < len(text) and text[index].isspace():
        index += 1
    return index if index < len(text) else None


def _is_call_parenthesis(text: str, opening: int) -> bool:
    previous = _previous_nonspace(text, opening)
    if previous is None:
        return False
    char = text[previous]
    return char.isalnum() or char in "_$)]"


def _literal_is_concatenated(text: str, opening: int, closing: int) -> bool:
    left, right = opening, closing
    while True:
        before = _previous_nonspace(text, left)
        after = _next_nonspace(text, right + 1)
        if (before is not None and text[before] == "+") or (
            after is not None and text[after] == "+"
        ):
            return True
        if (
            before is None
            or after is None
            or text[before] != "("
            or text[after] != ")"
            or _is_call_parenthesis(text, before)
        ):
            return False
        left, right = before, after


def _sql_concat(line: str) -> bool:
    """A SQL-bearing string literal that is an operand of `+`.

    "concatenated with `+`" is a claim about the operator's operands, so the `+`
    has to touch the literal carrying the keyword. Without that, a line like
    `"SELECT"; total = a + b` reads as SQL concatenation when it is nothing of
    the kind.
    """
    literals, _, comments_masked = _scan_source(line)
    for opening, start, end, closing in literals:
        if not SQL_KEYWORD_RE.search(line[start:end]):
            continue
        if _literal_is_concatenated(comments_masked, opening, closing):
            return True
    return False


def _line_rules(text: str) -> list[str]:
    """Rule ids triggered by a single added line."""
    hits = []
    if "eval(" in text:
        hits.append("MOCK-001")
    if CREDENTIAL_RE.search(text):
        hits.append("MOCK-002")
    if _sql_concat(text):
        hits.append("MOCK-003")
    if "== null" in text or "!= null" in text:
        hits.append("MOCK-005")
    if "JSON.parse(JSON.stringify(" in text:
        hits.append("MOCK-006")
    if "console.log(" in text:
        hits.append("MOCK-007")
    # Case-sensitive: `todo` appears in ordinary identifiers, `TODO` is the marker.
    if "TODO" in text or "FIXME" in text:
        hits.append("MOCK-008")
    lowered = text.lower()
    if any(p in lowered for p in INJECTION_PHRASES):
        hits.append("MOCK-INJ")
    return hits


def _empty_catch_findings(path: str, hunk: Hunk) -> list[Finding]:
    """MOCK-004: reconstruct the hunk's new-file text so a catch block whose body
    spans lines (including unchanged ones) is still detected. The finding is
    reported only when the `catch` line itself was added."""
    new_lines = [ln for ln in hunk.lines if ln.kind in (" ", "+")]
    if not new_lines:
        return []

    starts, pos = [], 0
    for ln in new_lines:
        starts.append(pos)
        pos += len(ln.text) + 1
    text = "\n".join(ln.text for ln in new_lines)
    _, syntax, _ = _scan_source(text)

    out = []
    for m in EMPTY_CATCH_RE.finditer(syntax):
        ln = new_lines[bisect_right(starts, m.start(1)) - 1]
        if ln.kind == "+":
            severity, category, title = RULES["MOCK-004"]
            out.append(
                Finding("MOCK-004", path, ln.new_line, severity, category, title, ln.text)
            )
    return out


def scan_files(files: list[FileDiff]) -> list[Finding]:
    out: list[Finding] = []
    for f in files:
        if f.binary:
            continue
        for hunk in f.hunks:
            out.extend(_empty_catch_findings(f.path, hunk))
            for ln in hunk.lines:
                if ln.kind != "+":
                    continue
                for rule_id in _line_rules(ln.text):
                    severity, category, title = RULES[rule_id]
                    out.append(
                        Finding(rule_id, f.path, ln.new_line, severity, category, title, ln.text)
                    )
    return out


class MockProvider:
    name = "mock"

    async def scan(self, files: list[FileDiff]) -> list[Finding]:
        # Regex work is synchronous and CPU-bound; keep it off the event loop so
        # streaming, health checks and rate limiting stay responsive under load.
        return await asyncio.to_thread(scan_files, files)
