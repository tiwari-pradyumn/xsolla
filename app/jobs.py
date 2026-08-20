"""Job lifecycle, the append-only event log behind SSE, and the caching /
idempotency / concurrency rules.

Everything lives in memory (see docs/adr/0002-in-memory-state.md). The event log
is what makes stream replay exact: a stream is always "replay the log from index
0, then follow it", so a consumer attaching to a finished job sees byte-identical
events to one that watched it live.
"""

import asyncio
import uuid
from collections import OrderedDict
from dataclasses import dataclass

from app.chunking import chunk_files
from app.config import MAX_CONCURRENT_JOBS
from app.diff import FileDiff, ParsedDiff
from app.findings import Finding, normalize
from app.providers import ProviderError, get_provider

TERMINAL = ("done", "failed")
MAX_RETAINED_JOBS = 20000


@dataclass(frozen=True)
class Event:
    name: str
    data: dict


class Job:
    def __init__(self, job_id: str, input_bytes: int, chunks: int, cache_hit: bool):
        self.id = job_id
        self.status = "queued"
        self.findings: list[Finding] = []
        self.usage = {"inputBytes": input_bytes, "chunks": chunks, "cacheHit": cache_hit}
        self.error: dict | None = None
        self.events: list[Event] = []
        self.closed = False
        self._change = asyncio.Event()
        self.finished = asyncio.Event()
        self._emit_status()

    # --- event log -------------------------------------------------------

    def _notify(self) -> None:
        self._change.set()
        self._change = asyncio.Event()

    def _emit(self, name: str, data: dict) -> None:
        self.events.append(Event(name, data))
        self._notify()

    def _emit_status(self) -> None:
        data = {"jobId": self.id, "status": self.status}
        if self.error:
            data["error"] = self.error
        self._emit("status", data)

    async def iter_events(self):
        """Replay the log from the start, then follow it until the job closes."""
        idx = 0
        while True:
            while idx < len(self.events):
                yield self.events[idx]
                idx += 1
            if self.closed:
                return
            change = self._change
            await change.wait()

    def _close(self) -> None:
        self._emit("done", {"total": len(self.findings), "usage": dict(self.usage)})
        self.closed = True
        self._notify()
        self.finished.set()

    # --- transitions -----------------------------------------------------

    def start(self) -> None:
        self.status = "running"
        self._emit_status()

    def finish(self, findings: list[Finding]) -> None:
        self.findings = findings
        for f in findings:
            self._emit("finding", f.to_dict())
        self.status = "done"
        self._emit_status()
        self._close()

    def fail(self, code: str, message: str) -> None:
        self.error = {"code": code, "message": message}
        self.status = "failed"
        self._emit_status()
        self._close()

    def to_dict(self) -> dict:
        body: dict = {"jobId": self.id, "status": self.status}
        if self.status == "done":
            body["findings"] = [f.to_dict() for f in self.findings]
        if self.status == "failed" and self.error:
            body["error"] = self.error
        body["usage"] = dict(self.usage)
        return body


class JobStore:
    def __init__(self, max_concurrent: int = MAX_CONCURRENT_JOBS):
        self.jobs: OrderedDict[str, Job] = OrderedDict()
        self._cache: dict[str, str] = {}  # cache key -> job id
        self._idempotency: dict[str, tuple[str, str]] = {}  # key -> (body hash, job id)
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._tasks: set[asyncio.Task] = set()

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def idempotent_replay(self, key: str, body_hash: str) -> Job | None:
        """Return the job this key already created, or None. Raises KeyError-free
        signalling via ConflictError when the same key was used with a different body."""
        entry = self._idempotency.get(key)
        if entry is None:
            return None
        stored_hash, job_id = entry
        if stored_hash != body_hash:
            raise IdempotencyConflict()
        job = self.jobs.get(job_id)
        if job is None:  # evicted; treat the key as fresh
            del self._idempotency[key]
            return None
        return job

    def submit(
        self,
        *,
        parsed: ParsedDiff,
        provider_name: str,
        max_findings: int,
        cache_key: str,
        idem_key: str | None,
        body_hash: str | None,
    ) -> Job:
        chunks = chunk_files(parsed.files)

        source_id = self._cache.get(cache_key)
        source = self.jobs.get(source_id) if source_id else None

        job = Job(
            job_id=uuid.uuid4().hex,
            input_bytes=parsed.input_bytes,
            chunks=len(chunks),
            cache_hit=source is not None,
        )
        self._register(job)

        if source is not None:
            self._spawn(self._run_cached(job, source))
        else:
            self._cache[cache_key] = job.id
            self._spawn(self._run_scan(job, chunks, provider_name, max_findings, cache_key))

        if idem_key and body_hash:
            self._idempotency[idem_key] = (body_hash, job.id)
        return job

    # --- internals -------------------------------------------------------

    def _register(self, job: Job) -> None:
        self.jobs[job.id] = job
        while len(self.jobs) > MAX_RETAINED_JOBS:
            for job_id, candidate in self.jobs.items():
                if candidate.status in TERMINAL:
                    del self.jobs[job_id]
                    break
            else:
                break

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run_scan(
        self,
        job: Job,
        chunks: list[list[FileDiff]],
        provider_name: str,
        max_findings: int,
        cache_key: str,
    ) -> None:
        # The semaphore is what caps concurrent processing: a fifth job simply
        # stays `queued` here rather than being rejected.
        async with self._semaphore:
            job.start()
            provider = get_provider(provider_name)
            collected: list[Finding] = []
            try:
                for chunk in chunks:
                    collected.extend(await provider.scan(chunk))
            except ProviderError as exc:
                self._cache.pop(cache_key, None)
                job.fail("provider_error", str(exc))
                return
            except Exception as exc:  # never let a bad scan take the process down
                self._cache.pop(cache_key, None)
                job.fail("internal", f"Scan failed: {type(exc).__name__}: {exc}")
                return
            job.finish(normalize(collected)[:max_findings])

    async def _run_cached(self, job: Job, source: Job) -> None:
        # No semaphore: this job performs no work, it only waits for the scan it
        # is reusing. Taking a slot here could deadlock against that very scan.
        job.start()
        await source.finished.wait()
        if source.status == "done":
            job.finish(list(source.findings))
        else:
            err = source.error or {"code": "internal", "message": "Source job failed."}
            job.fail(err["code"], err["message"])


class IdempotencyConflict(Exception):
    pass
