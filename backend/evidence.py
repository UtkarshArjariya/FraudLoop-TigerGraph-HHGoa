"""Durable human-evidence requests and exactly-once response attribution."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import fcntl
import uuid

from backend.data import atomic_json, read_json, utc_now


class PendingEvidence:
    def __init__(self, repository):
        self.path = repository.data / "pending_evidence.json"
        self.signals: dict[str, asyncio.Event] = {}

    @contextmanager
    def locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix(".lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    def all(self):
        value = read_json(self.path, {})
        return value if isinstance(value, dict) else {}

    def get(self, case_id):
        return self.all().get(case_id)

    def public(self, case_id):
        value = self.get(case_id)
        if not value or value.get("status") == "completed":
            return None
        return {key: item for key, item in value.items() if key != "snapshot"}

    def create(self, case_id, run_id, snapshot, request):
        with self.locked():
            records = self.all()
            previous = records.get(case_id)
            if previous and previous["run_id"] == run_id:
                return previous
            if previous and previous.get("status") != "completed":
                raise ValueError("Another request is already pending for this case.")
            record = {**request, "case_id": case_id, "run_id": run_id,
                      "request_id": uuid.uuid4().hex, "requested_at": utc_now(),
                      "status": "pending", "snapshot": snapshot, "response": None}
            records[case_id] = record
            atomic_json(self.path, records)
            self.signals[record["request_id"]] = asyncio.Event()
            return record

    def submit(self, case_id, request_id, response_type, note, answered_by):
        with self.locked():
            records = self.all()
            record = records.get(case_id)
            if not record or record.get("request_id") != request_id:
                raise ValueError("This evidence request is stale or does not exist.")
            expected = {"request_id": request_id, "response_type": response_type,
                        "note": note.strip(), "answered_by": answered_by}
            if record.get("response"):
                if any(record["response"].get(key) != value for key, value in expected.items()):
                    raise ValueError("This request already has a different response.")
                return record
            record["response"] = {**expected, "answered_at": utc_now(), "source": "human"}
            record["status"] = "answered"
            atomic_json(self.path, records)
        self.signals.setdefault(request_id, asyncio.Event()).set()
        return record

    async def wait(self, case_id, request_id):
        signal = self.signals.setdefault(request_id, asyncio.Event())
        while True:
            record = self.get(case_id)
            if not record or record["request_id"] != request_id:
                raise ValueError("Evidence request changed while paused.")
            if record.get("response"):
                return record
            # A bounded poll also notices a response written by another worker;
            # the supported demo deployment still runs a single API worker.
            try:
                await asyncio.wait_for(signal.wait(), timeout=5)
            except TimeoutError:
                continue

    def complete(self, case_id, run_id):
        with self.locked():
            records = self.all()
            record = records.get(case_id)
            if not record or record["run_id"] != run_id:
                return
            if record.get("status") == "completed":
                return
            record.update(status="completed", completed_at=utc_now())
            record.pop("snapshot", None)
            atomic_json(self.path, records)
