"""Split a parsed diff into chunks of at most CHUNK_BYTES, on file boundaries only.

A file's diff never spans two chunks; a single file larger than the limit becomes
its own chunk. Because every FileDiff already carries its own path and new-file
line numbers, chunk results merge without any positional fixup — which is why a
chunked scan is identical to an unchunked one.
"""

from app.config import CHUNK_BYTES
from app.diff import FileDiff


def chunk_files(files: list[FileDiff], limit: int = CHUNK_BYTES) -> list[list[FileDiff]]:
    chunks: list[list[FileDiff]] = []
    current: list[FileDiff] = []
    size = 0

    for f in files:
        if current and size + f.raw_bytes > limit:
            chunks.append(current)
            current, size = [], 0
        current.append(f)
        size += f.raw_bytes
        if size > limit:  # a single oversized file, alone in this chunk
            chunks.append(current)
            current, size = [], 0

    if current:
        chunks.append(current)
    return chunks
