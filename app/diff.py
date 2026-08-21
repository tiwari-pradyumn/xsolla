"""Unified-diff parser.

Git extended headers, bare `diff -u` output, binary sections and
`\\ No newline at end of file` markers are tolerated. Hunk line counts are
validated because an under- or over-filled hunk is not a parseable unified diff.

Each parsed file carries the exact byte span of the original diff it came from,
so chunking can split on file boundaries without re-serialising anything.
"""

import re
from dataclasses import dataclass, field

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
GIT_HEADER_RE = re.compile(r"^diff --git (?:a/)?(.+?) (?:b/)?(.+)$")


@dataclass
class Line:
    kind: str  # ' ' context, '+' added, '-' removed
    text: str  # content with the marker and line terminator stripped
    new_line: int | None  # line number in the new file, None for removals


@dataclass
class Hunk:
    new_start: int
    lines: list[Line] = field(default_factory=list)


@dataclass
class FileDiff:
    path: str
    hunks: list[Hunk] = field(default_factory=list)
    binary: bool = False
    raw_text: str = ""
    raw_bytes: int = 0

    @property
    def added_lines(self) -> list[Line]:
        return [ln for h in self.hunks for ln in h.lines if ln.kind == "+"]


@dataclass
class ParsedDiff:
    files: list[FileDiff]
    input_bytes: int


class InvalidDiff(Exception):
    pass


def _strip_eol(s: str) -> str:
    if s.endswith("\n"):
        s = s[:-1]
    if s.endswith("\r"):
        s = s[:-1]
    return s


def _clean_path(p: str) -> str:
    p = p.split("\t", 1)[0].strip()
    if len(p) >= 2 and p.startswith('"') and p.endswith('"'):
        p = p[1:-1]
    if p == "/dev/null":
        return p
    if p.startswith("a/") or p.startswith("b/"):
        p = p[2:]
    return p


class _Builder:
    def __init__(self, start_idx: int):
        self.start_idx = start_idx
        self.old_path: str | None = None
        self.new_path: str | None = None
        self.git_path: str | None = None
        self.hunks: list[Hunk] = []
        self.binary = False

    def resolve_path(self) -> str:
        for candidate in (self.new_path, self.old_path, self.git_path):
            if candidate and candidate != "/dev/null":
                return candidate
        return "unknown"


def parse_diff(text: str) -> ParsedDiff:
    raw_lines = text.splitlines(keepends=True)
    byte_lens = [len(ln.encode("utf-8")) for ln in raw_lines]

    builders: list[_Builder] = []
    cur: _Builder | None = None
    hunk: Hunk | None = None
    old_rem = new_rem = 0
    new_lineno = 0

    def start_file(idx: int) -> _Builder:
        nonlocal cur, hunk, old_rem, new_rem
        cur = _Builder(idx)
        builders.append(cur)
        hunk, old_rem, new_rem = None, 0, 0
        return cur

    for i, raw in enumerate(raw_lines):
        line = _strip_eol(raw)
        in_hunk = hunk is not None and (old_rem > 0 or new_rem > 0)

        if in_hunk:
            marker = line[:1]
            if marker == "\\":  # "\ No newline at end of file"
                continue
            if marker in (" ", "+", "-"):
                kind = marker
                content = line[1:]
                if kind == "-":
                    if old_rem == 0:
                        raise InvalidDiff("Malformed hunk: too many removed lines.")
                    hunk.lines.append(Line("-", content, None))
                    old_rem -= 1
                elif kind == "+":
                    if new_rem == 0:
                        raise InvalidDiff("Malformed hunk: too many added lines.")
                    hunk.lines.append(Line(kind, content, new_lineno))
                    new_lineno += 1
                    new_rem -= 1
                else:
                    if old_rem == 0 or new_rem == 0:
                        raise InvalidDiff("Malformed hunk: too many context lines.")
                    hunk.lines.append(Line(kind, content, new_lineno))
                    new_lineno += 1
                    old_rem -= 1
                    new_rem -= 1
                continue
            raise InvalidDiff("Malformed hunk: body ended before its declared line counts.")

        if hunk is not None and old_rem == 0 and new_rem == 0:
            extra_body_line = line.startswith(" ") or (
                line.startswith("+") and not line.startswith("+++ ")
            ) or (line.startswith("-") and not line.startswith("--- "))
            if extra_body_line:
                raise InvalidDiff("Malformed hunk: body exceeds its declared line counts.")

        if line.startswith("diff --git "):
            b = start_file(i)
            m = GIT_HEADER_RE.match(line)
            if m:
                b.git_path = _clean_path(m.group(2))
            continue

        if line.startswith("--- "):
            # A bare `diff -u` stream starts a new file here.
            if cur is None or cur.hunks or cur.binary or cur.old_path is not None:
                start_file(i)
            cur.old_path = _clean_path(line[4:])
            continue

        if line.startswith("+++ "):
            if cur is None:
                start_file(i)
            cur.new_path = _clean_path(line[4:])
            continue

        m = HUNK_RE.match(line)
        if m:
            if cur is None:
                start_file(i)
            new_start = int(m.group(3))
            old_rem = int(m.group(2)) if m.group(2) is not None else 1
            new_rem = int(m.group(4)) if m.group(4) is not None else 1
            new_lineno = new_start
            hunk = Hunk(new_start=new_start)
            cur.hunks.append(hunk)
            # A zero-length hunk header carries no body.
            if old_rem == 0 and new_rem == 0:
                hunk = None
            continue

        if line.startswith("Binary files ") or line.startswith("GIT binary patch"):
            if cur is None:
                start_file(i)
            cur.binary = True
            continue

    if hunk is not None and (old_rem > 0 or new_rem > 0):
        raise InvalidDiff("Malformed hunk: body ended before its declared line counts.")

    if not builders:
        raise InvalidDiff("No file sections found.")

    # Tile the whole input across the file sections so byte accounting is exact:
    # anything before the first section header belongs to that first section.
    bounds = [b.start_idx for b in builders]
    bounds[0] = 0
    bounds.append(len(raw_lines))

    files: list[FileDiff] = []
    for idx, b in enumerate(builders):
        start, end = bounds[idx], bounds[idx + 1]
        files.append(
            FileDiff(
                path=b.resolve_path(),
                hunks=b.hunks,
                binary=b.binary,
                raw_text="".join(raw_lines[start:end]),
                raw_bytes=sum(byte_lens[start:end]),
            )
        )

    if not any(f.hunks or f.binary for f in files):
        raise InvalidDiff("No hunks found; input is not a unified diff.")

    return ParsedDiff(files=files, input_bytes=len(text.encode("utf-8")))
