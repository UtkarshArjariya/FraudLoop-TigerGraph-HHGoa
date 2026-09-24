"""Optional investigation observers and portable, dependency-free replay files.

Observers never own investigation decisions. They receive copies of JSON payloads;
an observer failure is logged without changing the underlying investigation.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import tempfile
import time
from typing import Callable


EventCallback = Callable[[str, dict], None]
EVENT_TYPES = frozenset({
    "step_started", "tool_call", "evidence_added", "assessment", "nba_initial",
    "evidence_requested", "evidence_received", "nba_final", "sar_drafted",
    "case_written", "done", "error",
})
LOG = logging.getLogger(__name__)


def safe_emitter(callback: EventCallback | None) -> EventCallback:
    if callback is None:
        return lambda _event_type, _payload: None

    def emit(event_type: str, payload: dict) -> None:
        try:
            # A UI observer must not mutate actions/evidence used by the core.
            callback(event_type, json.loads(json.dumps(payload, allow_nan=False)))
        except Exception as exc:
            LOG.warning("investigation_observer_failed event=%s error=%s", event_type, type(exc).__name__)

    return emit


class EventRecorder:
    """A synchronous callback suitable for investigate(..., emit=recorder.emit)."""

    def __init__(self, case_id: str, *, source: str = "live"):
        self.case_id = case_id
        self.source = source
        self.events: list[dict] = []
        self.started = time.perf_counter()

    def emit(self, event_type: str, payload: dict) -> None:
        if event_type not in EVENT_TYPES:
            raise ValueError(f"Unknown investigation event: {event_type}")
        self.events.append({
            "event_type": event_type, "case_id": self.case_id,
            "sequence": len(self.events) + 1,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "elapsed_ms": round((time.perf_counter() - self.started) * 1000, 3),
            "source": self.source,
            "payload": json.loads(json.dumps(payload, allow_nan=False)),
        })


def write_timeline(path: Path | str, events: list[dict]) -> None:
    """Atomically publish a complete stream; concurrent writers use unique temps."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(events, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def read_timeline(path: Path | str) -> list[dict]:
    """Load an event list without initializing data stores, SDKs, or graph clients."""
    events = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(events, list) or any(
        not isinstance(event, dict) or event.get("event_type") not in EVENT_TYPES
        or not isinstance(event.get("payload"), dict) for event in events
    ):
        raise ValueError("Timeline must be a list of investigation event envelopes")
    return events


def args_summary(params: dict) -> dict:
    """Summarize only query context; never expose vectors, answer text, or secrets."""
    visible = {"card_id", "case_id", "customer_id", "device_id", "region_id", "txn_id",
               "as_of", "opened_epoch", "hours", "days", "k", "embed_model", "attribute",
               "vertex_type", "verdict"}
    summary = {key: value for key, value in params.items() if key in visible}
    for key in ("query_vec", "covered_ids", "vectors"):
        if isinstance(params.get(key), list):
            summary[f"{key}_count"] = len(params[key])
    return summary


def result_row_counts(result) -> tuple[int | None, dict[str, int]]:
    """Count returned collections, avoiding known aliases and nested graph paths."""
    if isinstance(result, list):
        return len(result), {"rows": len(result)}
    if not isinstance(result, dict):
        return None, {}
    keys = ["transactions", "memory", "documents", "components"]
    keys.append("hits" if isinstance(result.get("hits"), list) else "cases")
    if "transactions" not in result:
        keys.append("rows")
    counts = {key: len(result[key]) for key in keys if isinstance(result.get(key), list)}
    if counts:
        return sum(counts.values()), counts
    if isinstance(result.get("success_count"), int):
        return result["success_count"], {"successful_writes": result["success_count"]}
    # A write acknowledgement contains no returned row collection.
    return None, {}


class ObservedGraph:
    """Transparent graph proxy measuring existing calls, including retrieval calls."""

    def __init__(self, graph, emit: EventCallback):
        self._graph, self._emit = graph, emit

    def __getattr__(self, name):
        return getattr(self._graph, name)

    async def _call(self, tool: str, params: dict, operation):
        started = time.perf_counter()
        try:
            result = await operation()
        except Exception as exc:
            self._emit("tool_call", {"tool": tool, "args_summary": args_summary(params),
                       "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                       "rows": None, "row_counts": {}, "success": False,
                       "error_type": type(exc).__name__,
                       "backend": "local" if self.use_local else "tigergraph-mcp"})
            raise
        rows, counts = result_row_counts(result)
        self._emit("tool_call", {"tool": tool, "args_summary": args_summary(params),
                   "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                   "rows": rows, "row_counts": counts, "success": True,
                   "backend": "local" if self.use_local else "tigergraph-mcp"})
        return result

    async def call(self, name: str, params: dict | None = None):
        return await self._call(name, params or {}, lambda: self._graph.call(name, params))

    async def upsert_vectors(self, vertex_type: str, vectors: list[dict], *, attribute="embedding"):
        return await self._call("upsert_vectors", {"vertex_type": vertex_type,
                                "vectors": vectors, "attribute": attribute},
                                lambda: self._graph.upsert_vectors(vertex_type, vectors, attribute=attribute))


def evidence_request_reason(request_type: str, actions: list[dict]) -> str:
    action_name = {"customer_validation": "VERIFY_WITH_CUSTOMER", "step_up_auth": "STEP_UP_AUTH",
                   "analyst_info": "ESCALATE_TO_ANALYST"}.get(request_type)
    reasons = [action.get("reason", "") for action in actions if action.get("action") == action_name]
    return " ".join(filter(None, reasons)) or {
        "customer_validation": "Resolve whether the cardholder authorized the activity.",
        "step_up_auth": "Resolve authorization on the newly observed device profile.",
        "analyst_info": "Resolve conflicting evidence or uncertain high exposure.",
    }.get(request_type, "Reason was not captured in the saved artifacts.")
