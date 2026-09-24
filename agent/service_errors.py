"""Shared redacted API diagnostics and retry metadata parsing."""
import math
import os
import re

def exception_message(exc: Exception) -> str:
    """Keep the full service error, including status/details, but redact keys."""
    message = str(exc)
    prefix = " ".join(str(value) for value in (getattr(exc, "code", ""), getattr(exc, "status", "")) if value)
    if prefix and not message.startswith(prefix):
        message = f"{prefix}: {message}"
    for name in ("GEMINI_API_KEY", "GEMINI_EMBEDDING", "HF_TOKEN"):
        secret = os.getenv(name, "")
        if secret:
            message = message.replace(secret, "[REDACTED]")
    return re.sub(r"(?i)([?&](?:key|api_key)=)[^&\s'\"]+", r"\1[REDACTED]", message)


def status_code(exc: Exception) -> int | None:
    for value in (getattr(exc, "code", None), getattr(getattr(exc, "response", None), "status_code", None)):
        try:
            return int(value)
        except (ValueError, TypeError):
            pass
    match = re.search(r"\b(400|429|500|502|503|504)\b", str(exc))
    return int(match[1]) if match else None


def retry_delay(exc: Exception) -> float | None:
    def seconds(value):
        try:
            if isinstance(value, dict):
                delay = float(value.get("seconds", 0)) + float(value.get("nanos", 0)) / 1e9
            else:
                delay = float(str(value).removesuffix("s"))
            return delay if math.isfinite(delay) and delay >= 0 else None
        except (TypeError, ValueError):
            return None

    def visit(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in ("retryDelay", "retry_delay"):
                    found = seconds(child)
                    if found is not None:
                        return found
                found = visit(child)
                if found is not None:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = visit(child)
                if found is not None:
                    return found
        return None

    for payload in (getattr(exc, "details", None), getattr(exc, "response_json", None)):
        found = visit(payload)
        if found is not None:
            return found
    match = re.search(r"retry_?delay['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)s?", str(exc), re.I)
    return seconds(match[1]) if match else None



def rate_limited(exc):
    return status_code(exc) == 429 or "RESOURCE_EXHAUSTED" in (str(exc) + str(getattr(exc, "status", ""))).upper()

def transient(exc):
    return rate_limited(exc) or status_code(exc) == 503 or "UNAVAILABLE" in (str(exc) + str(getattr(exc, "status", ""))).upper()
