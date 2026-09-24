"""Two process-owned SDK clients; credentials never enter logs or artifacts."""
from functools import lru_cache
import hashlib
import logging
import os

from dotenv import load_dotenv
from google import genai
from google.genai import types

logger = logging.getLogger(__name__)


def api_key(kind="gen"):
    load_dotenv()
    if kind == "embed":
        key = os.getenv("GEMINI_EMBEDDING", "").strip()
        if key:
            return key
        _warn_missing_embedding_key()
    return os.getenv("GEMINI_API_KEY", "").strip()


@lru_cache(maxsize=1)
def _warn_missing_embedding_key():
    logger.warning("GEMINI_EMBEDDING is missing; embedding calls use GEMINI_API_KEY and share its project quota.")


def configured(kind="gen"):
    key = api_key(kind)
    return len(key) > 20 and not any(word in key.lower() for word in ("your_", "replace", "placeholder"))


def credential_scope(kind):
    # Used only for cache invalidation/quota isolation; never store credentials.
    return hashlib.sha256((kind + ":" + api_key(kind)).encode()).hexdigest()[:24]


@lru_cache(maxsize=2)
def _client(kind, key):
    return genai.Client(api_key=key, http_options=types.HttpOptions(
        timeout=30_000, retry_options=types.HttpRetryOptions(attempts=1)))


def get_client(kind="gen"):
    if kind not in ("gen", "embed"):
        raise ValueError("Unknown model client role")
    return _client(kind, api_key(kind))
