"""Persistent model quota cooldowns, partitioned by credential role/scope."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import json
import logging
import os
from pathlib import Path
import time
from zoneinfo import ZoneInfo

from agent.model_clients import credential_scope
from agent.model_registry import atomic_json
from agent.service_errors import retry_delay

logger = logging.getLogger(__name__)


def quota_ids(exc):
    found = []

    def visit(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in ("quotaId", "quota_id") and isinstance(child, str):
                    found.append(child)
                else:
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    for payload in (getattr(exc, "details", None), getattr(exc, "response_json", None)):
        visit(payload)
    return sorted(set(found))


def next_pacific_midnight(now):
    local = datetime.fromtimestamp(now, ZoneInfo("America/Los_Angeles"))
    return datetime.combine(local.date() + timedelta(days=1), datetime.min.time(),
                            tzinfo=ZoneInfo("America/Los_Angeles")).timestamp()


class QuotaState:
    def __init__(self, kind="gen", *, path=None, scope=None, clock=None):
        self.path = Path(path or os.getenv("QUOTA_STATE_PATH", "data/quota_state.json"))
        self.scope = scope or credential_scope(kind)
        self.clock = clock or time.time
        self._memory = {}

    @contextmanager
    def _lock(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix(".lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _read(self):
        try:
            value = json.loads(self.path.read_text())
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def remaining(self, model):
        scoped = self._read().get(self.scope, {})
        entry = scoped.get(model, self._memory.get(model, {})) if isinstance(scoped, dict) else self._memory.get(model, {})
        try:
            return max(0, float(entry.get("until", 0)) - self.clock())
        except (TypeError, ValueError, AttributeError):
            return 0

    def record(self, model, exc):
        ids = quota_ids(exc)
        daily = any("perday" in name.lower() for name in ids)
        now = self.clock()
        delay = retry_delay(exc)
        until = next_pacific_midnight(now) if daily else now + (30 if delay is None else delay)
        entry = {"until": until, "kind": "daily" if daily else "minute", "quota_ids": ids,
                 "reset_at": datetime.fromtimestamp(until, timezone.utc).isoformat()}
        self._memory[model] = entry
        try:
            with self._lock():
                state = self._read()
                previous = state.setdefault(self.scope, {}).get(model, {})
                if previous.get("until", 0) > until:
                    entry = previous
                state[self.scope][model] = entry
                atomic_json(self.path, state)
        except OSError:
            logger.warning("Quota state could not be saved; cooldown is retained in memory.")
        return entry
