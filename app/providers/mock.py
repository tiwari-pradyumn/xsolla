"""The deterministic mock provider: the finding-rules table, implemented exactly.

Rules apply to added lines only. `line` is the line number in the new file.
Interpretation calls that the table leaves open are noted per rule and listed in
SUBMISSION.md.
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

# Whitespace-only body; the catch binding is optional (JS allows `catch {}`).
EMPTY_CATCH_RE = re.compile(r"catch\s*(?:\([^)]*\))?\s*\{\s*\}")

INJECTION_PHRASES = (
    "ignore previous instructions",
    "disregard all prior",
    "you are now",
)


def _split_strings(s: str) -> tuple[list[str], bool]:
    """Return string-literal contents and whether a `+` occurs outside them."""
    literals: list[str] = []
    plus_outside = False
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c in ("'", '"', "`"):
            quote, i = c, i + 1
            buf: list[str] = []
            while i < n:
                if s[i] == "\\" and i + 1 < n:
                    buf.append(s[i : i + 2])
                    i += 2
                    continue
                if s[i] == quote:
                    break
                buf.append(s[i])
                i += 1
            literals.append("".join(buf))
            i += 1  # past the closing quote
        else:
            if c == "+":
                plus_outside = True
            i += 1
    return literals, plus_outside


def _sql_concat(line: str) -> bool:
    literals, plus_outside = _split_strings(line)
    if not plus_outside:
        return False
    return any(SQL_KEYWORD_RE.search(lit) for lit in literals)


def _line_rules(text: str) -> list[str]:
    """Rule ids triggered by a single added line."""
    hits = []
    if "eval(" in text:
        hits.append("MOCK-001")
    if CREDENTIAL_RE.search(text):
        hits.append("MOCK-002")
    if _sql_concat(text):
        hits.append("MOCK-003")
    # Literal trigger, same shape as MOCK-001's `eval(`: the table says
    # `== null` or `!= null`, and the table is scored exactly. Knowingly
    # fires on `=== null` (it contains `== null`) and skips `x==null` (no
    # space) -- the title says "loose", but the trigger column binds.
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

    out = []
    for m in EMPTY_CATCH_RE.finditer(text):
        ln = new_lines[bisect_right(starts, m.start()) - 1]
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
