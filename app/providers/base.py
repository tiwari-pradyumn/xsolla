from typing import Protocol

from app.diff import FileDiff
from app.findings import Finding


class ProviderError(Exception):
    """A provider could not produce findings. Fails the job, never the process."""


class Provider(Protocol):
    name: str

    async def scan(self, files: list[FileDiff]) -> list[Finding]:
        """Scan one chunk (a list of whole file diffs) and return its findings."""
        ...
