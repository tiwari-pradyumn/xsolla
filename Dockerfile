FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000

# Exactly one worker: jobs, cache, idempotency keys and SSE event logs are
# in-process state (docs/adr/0002-in-memory-state.md). A second worker would
# serve 404s for another worker's jobs and break caching and idempotency.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
