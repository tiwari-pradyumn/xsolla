import os

from dotenv import load_dotenv

# Populate os.environ from a local .env before anything is read below. Values
# already present in the real environment win, so a container's injected
# secrets are never overridden by a stray .env, and tests that set variables
# before import keep control. Absent a .env this is a no-op.
load_dotenv()

VERSION = "1.0.0"
SPEC_VERSION = "1.0"

MAX_PAYLOAD_BYTES = 1048576
CHUNK_BYTES = 65536
MAX_CONCURRENT_JOBS = 4
RATE_LIMIT_PER_MINUTE = 30

DEFAULT_MAX_FINDINGS = 100

# No default. Every /v1 route depends on this one value, so an unset token must
# stop the process rather than fall back to something published in this repo:
# a misconfigured deploy should fail loudly in the logs, not serve an open API
# that looks perfectly healthy.
AUTH_TOKEN = os.environ.get("AUTH_TOKEN", "").strip()
if not AUTH_TOKEN:
    raise RuntimeError(
        "AUTH_TOKEN is unset. Refusing to start: every /v1 route would be "
        "unprotected. Set AUTH_TOKEN in the environment, or in .env locally."
    )

# Never hardcode a default here: this file is committed. Credentials come from
# the environment, or from .env locally, which is gitignored.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "gemini-3.5-flash-lite")
LLM_TIMEOUT_SECONDS = float(os.environ.get("LLM_TIMEOUT_SECONDS", "20"))
