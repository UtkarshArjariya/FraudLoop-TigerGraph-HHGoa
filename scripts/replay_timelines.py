"""Reconstruct disclosed demos from saved answers/traces, without running the agent.

Historical traces contain state offsets and assessment families, but not individual
tool durations or result counts. Such missing observations remain null. This command
never opens transaction data, calls an API, loads an embedding model, or edits cases.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import re

from agent.events import evidence_request_reason, read_timeline, write_timeline


DISCLOSURE = (
    "Reconstructed replay from the saved answer and available trace; no investigation, "
    "LLM request, or graph operation runs during replay. State offsets are historical "
    "when available; individual tool execution order, durations, and row counts were "
    "not recorded. Query cards identify evidence references, not newly observed calls. "
    "Customer, step-up, and analyst replies remain disclosed deterministic simulations."
)


def reconstruct_timeline(answer: dict, trace: dict | None = None, trigger: dict | None = None) -> list[dict]:
    """Build a portable stream without treating missing historic telemetry as fact."""
    trace = trace or {}
    cid, result = answer["case_id"], answer["case"]
    trace_matches = trace.get("case_id", cid) == cid and trace.get(
        "final_probability", result["fraud_probability"]
    ) == result["fraud_probability"]
    if not trace_matches:
        trace = {}
    states = {row["state"]: row.get("elapsed_s") for row in trace.get("states", [])}
    events: list[dict] = []

    def emit(event_type: str, payload: dict, elapsed_s=None):
        events.append({"event_type": event_type, "case_id": cid, "sequence": len(events) + 1,
                       "timestamp": None,
                       "elapsed_ms": round(elapsed_s * 1000, 3) if elapsed_s is not None else None,
                       "source": "reconstructed", "payload": payload})

    def state(name: str, **extra):
        emit("step_started", {"step": name, "state": name, "elapsed_s": states.get(name),
                              "timing_source": "saved_trace" if name in states else "unavailable", **extra},
             states.get(name))

    state("TRIGGER", trigger=trigger or {"case_id": cid}, disclosure=DISCLOSURE,
          trace_available=bool(trace), trace_matches_answer=trace_matches)
    state("GATHER")
    # Only name queries explicitly cited by saved evidence. Never fabricate counts,
    # invocation arguments, success flags, or missing vector search observations.
    query_names = []
    for item in result.get("evidence", []):
        query_names.extend(re.findall(r"query:([a-z][a-z0-9_]*)", item.get("ref", "")))
    for name in dict.fromkeys(query_names):
        emit("tool_call", {"tool": name, "args_summary": {}, "duration_ms": None,
                           "rows": None, "row_counts": {}, "success": None,
                           "execution_observed": False, "order_observed": False,
                           "backend": trace.get("backend", "unavailable"),
                           "disclosure": "Query cited by saved evidence; execution order, timing, and row counts are unavailable."})
    state("ASSESS")
    for item in result.get("evidence", []):
        if not item.get("ref", "").startswith("evidence_request:"):
            emit("evidence_added", {"phase": "initial", **item})
    requests = answer.get("evidence_requests", [])
    initial_context = trace.get("initial_policy_context", {})
    emit("assessment", {"phase": "initial",
                        "probability": trace.get("initial_probability", result["fraud_probability"] if not requests else None),
                        "verdict": initial_context.get("verdict", result["verdict"] if not requests else None),
                        "family_contributions": trace.get("initial_families", {}),
                        "exposure_usd": initial_context.get("exposure"),
                        "source_detail": "saved_trace" if trace else "saved_answer; pre-request values may be unavailable"})
    state("INITIAL_NBA")
    actions = answer["next_best_actions"]
    emit("nba_initial", {"actions": actions["initial"]})
    if requests:
        state("REQUEST_EVIDENCE")
        for request in requests:
            emit("evidence_requested", {"type": request["type"],
                                        "reason": evidence_request_reason(request["type"], actions["initial"]),
                                        "precedent_ids": result.get("similar_prior_cases", []), "simulated": True})
            emit("evidence_received", {**request, "simulated": True,
                                       "disclosure": "Saved deterministic simulation; no new customer contact or analyst reply."})
        state("REASSESS")
        for item in result.get("evidence", []):
            if item.get("ref", "").startswith("evidence_request:"):
                emit("evidence_added", {"phase": "reassessment", **item})
        emit("assessment", {"phase": "final", "probability": result["fraud_probability"],
                            "verdict": result["verdict"], "pattern": result["pattern"],
                            "exposure_usd": result["exposure_usd"],
                            "family_contributions": trace.get("final_families", {})})
    state("FINAL_NBA")
    emit("nba_final", {"actions": actions["final"], "what_changed": actions["what_changed"]})
    state("SAR")
    emit("sar_drafted", {**answer["sar"], "recommendation_only": True,
                         "filing_status": "awaiting_l2_approval" if answer["sar"]["file"] else "not_recommended"})
    state("WRITE_BACK")
    emit("case_written", {"case_id": cid, "written_to_graph": result.get("written_to_graph", False),
                          "graph_case_id": result.get("graph_case_id", ""), "historical_writeback": True,
                          "disclosure": "Write-back status from the saved answer; replay performs no graph write and does not recheck the stored vertex."})
    state("ANSWER")
    emit("done", {"case_id": cid, "verdict": result["verdict"], "probability": result["fraud_probability"],
                  "status": result["status"], "sar": answer["sar"]["file"],
                  "written_to_graph": result.get("written_to_graph", False),
                  "tool_calls": answer.get("tool_calls"), "latency_s": answer.get("latency_s"),
                  "stop_reason": answer["stop_reason"], "answer": answer, "disclosure": DISCLOSURE})
    return events


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=Path("cases"))
    parser.add_argument("--traces", type=Path, default=Path("data/runs"))
    parser.add_argument("--case-pack", type=Path, default=Path("HHGOA_IEEE/case_pack.csv"))
    parser.add_argument("--output", type=Path, default=Path("data/timelines"))
    parser.add_argument("--overwrite-reconstructed", action="store_true",
                        help="Refresh reconstructed files; actual recorded streams are always preserved")
    args = parser.parse_args()
    triggers = {}
    if args.case_pack.exists():
        with args.case_pack.open(encoding="utf-8", newline="") as handle:
            triggers = {row["case_id"]: row for row in csv.DictReader(handle)}
    written, preserved = 0, 0
    for source in sorted(args.cases.glob("*.json")):
        answer = json.loads(source.read_text(encoding="utf-8"))
        cid = answer["case_id"]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", cid):
            raise ValueError(f"Unsafe timeline case ID: {cid!r}")
        path = args.output / f"{cid}.json"
        if path.exists():
            existing = read_timeline(path)
            if not args.overwrite_reconstructed or any(event.get("source") != "reconstructed" for event in existing):
                preserved += 1
                continue
        trace_path = args.traces / f"{cid}.trace.json"
        trace = json.loads(trace_path.read_text(encoding="utf-8")) if trace_path.exists() else {}
        write_timeline(path, reconstruct_timeline(answer, trace, triggers.get(cid)))
        written += 1
    print(f"Reconstructed {written} timelines; preserved {preserved} existing streams. No agent/API/graph calls or case edits.")


if __name__ == "__main__":
    main()
