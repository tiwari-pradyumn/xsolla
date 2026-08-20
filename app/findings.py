"""Finding value object plus the canonical ordering/dedup used everywhere."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Finding:
    rule_id: str
    path: str
    line: int
    severity: str
    category: str
    title: str
    evidence: str

    @property
    def id(self) -> str:
        return f"{self.rule_id}:{self.path}:{self.line}"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "ruleId": self.rule_id,
            "path": self.path,
            "line": self.line,
            "severity": self.severity,
            "category": self.category,
            "title": self.title,
            "evidence": self.evidence,
        }


def normalize(findings: list[Finding]) -> list[Finding]:
    """Deduplicate by id, then order by path, line, ruleId.

    Applied after merging chunk results, which is what makes a chunked scan
    indistinguishable from an unchunked one.
    """
    seen: dict[str, Finding] = {}
    for f in findings:
        seen.setdefault(f.id, f)
    return sorted(seen.values(), key=lambda f: (f.path, f.line, f.rule_id))
