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

AUTH_TOKEN = os.environ.get("AUTH_TOKEN", "dev-token")

# Never hardcode a default here: this file is committed. Credentials come from
# the environment, or from .env locally, which is gitignored.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "gemini-3.5-flash-lite")
LLM_TIMEOUT_SECONDS = float(os.environ.get("LLM_TIMEOUT_SECONDS", "20"))
