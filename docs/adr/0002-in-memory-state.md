# All service state lives in memory

Jobs, the result cache, idempotency keys, and SSE event logs are plain in-process dicts — no database. The contract requires no durability, the service is a single instance, and the scoring window is 48 hours; SQLite would add write-path plumbing for a failure mode we instead eliminate by deploying on a host that never scales to zero or restarts routinely. Accepted risk: a crash loses in-flight jobs.
