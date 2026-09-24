"""Observers preserve decisions; replay never invents missing historic telemetry."""
import asyncio
from copy import deepcopy
import importlib
import json
from pathlib import Path

import pytest

from agent.assess import Assessment
from agent.events import EventRecorder, ObservedGraph, args_summary, read_timeline, result_row_counts, safe_emitter, write_timeline
from scripts.replay_timelines import reconstruct_timeline


class FakeGraph:
    use_local = True

    def __init__(self, fail=None):
        self.tool_calls = 0
        self.calls = []
        self.fail = fail
        self.flag = {"txn_id": "T1", "epoch": 1477915200, "ts": "2016-10-31 12:00:00",
                     "amount": 40.0, "card_id": "C1-K1", "customer_id": "C1",
                     "device_profile": "Phone|OS|Browser|Display", "region": "101",
                     "channel": "online", "product": "C"}

    async def call(self, name, params=None):
        self.tool_calls += 1
        self.calls.append((name, deepcopy(params)))
        if name == self.fail:
            raise RuntimeError("synthetic graph failure")
        if name == "card_window":
            return {"transactions": [self.flag]}
        if name == "write_case":
            return {"written_to_graph": False, "stored_locally": True}
        return {"transactions": [], "cases": [], "documents": []}


@pytest.fixture
def investigation(monkeypatch):
    module = importlib.import_module("agent.investigate")
    graph = FakeGraph()
    initial = Assessment(.35, "uncertain", "none", "", [graph.flag], [], [],
                         [{"claim": "A synthetic observation", "source": "graph", "ref": "query:card_window", "entity_ids": ["T1"]}],
                         [{"case_id": "CC-1", "retrieval_method": "graph_fallback"}],
                         {"device": .4}, {"recurring_match": False, "card_testing": False,
                         "shared_fraud": False, "split_burst": False, "coordinated_device": False,
                         "evidence_conflicts": False, "new_device": True})
    monkeypatch.setattr(module, "assess", lambda *args, **kwargs: deepcopy(initial))
    monkeypatch.setattr(module, "index_specs", lambda *args: [])

    async def retrieval(observed_graph, *args):
        # Calls made inside retrieval must receive the same observation hooks.
        await observed_graph.call("similar_cases", {"query_vec": [0.] * 768, "covered_ids": ["CC-1"]})
        return {}, []

    monkeypatch.setattr(module, "retrieve_indexes", retrieval)
    case = {"case_id": "HHG-TEST", "opened_at": "2016-10-31 12:00:00", "card_id": "C1-K1",
            "customer_id": "C1", "flagged_txn_id": "T1", "trigger_type": "risk_score",
            "trigger_text": "Synthetic test alert"}
    return module, case


def decisions(answer):
    answer = deepcopy(answer)
    answer.pop("latency_s")
    return answer


def test_observer_records_flow_and_does_not_change_decisions(investigation):
    module, case = investigation
    baseline, _ = asyncio.run(module.investigate(case, FakeGraph()))
    graph = FakeGraph()
    recorder = EventRecorder(case["case_id"])
    answer, _ = asyncio.run(module.investigate(case, graph, emit=recorder.emit))
    assert decisions(answer) == decisions(baseline)
    events = recorder.events
    kinds = [event["event_type"] for event in events]
    assert {"step_started", "tool_call", "evidence_added", "assessment", "nba_initial", "evidence_requested",
            "evidence_received", "nba_final", "sar_drafted", "case_written", "done"} <= set(kinds)
    assert kinds.index("nba_initial") < kinds.index("evidence_requested") < kinds.index("evidence_received") < kinds.index("nba_final")
    assert kinds[-1] == "done"
    assert len([event for event in events if event["event_type"] == "tool_call"]) == graph.tool_calls
    calls = {event["payload"]["tool"]: event["payload"] for event in events if event["event_type"] == "tool_call"}
    assert calls["card_window"]["rows"] == 1
    assert calls["card_window"]["duration_ms"] >= 0
    assert calls["similar_cases"]["args_summary"] == {"query_vec_count": 768, "covered_ids_count": 1}
    assert calls["write_case"]["rows"] is None
    received = next(event["payload"] for event in events if event["event_type"] == "evidence_received")
    assert received["simulated"] is True and "SIMULATED" in received["assumed_response"]
    assert events[-1]["payload"]["answer"] == answer
    assert all(event["source"] == "live" for event in events)


def test_broken_or_mutating_observer_cannot_change_policy(investigation, caplog):
    module, case = investigation
    expected, _ = asyncio.run(module.investigate(case, FakeGraph()))

    def broken(_kind, _payload):
        raise RuntimeError("observer failed")

    actual, _ = asyncio.run(module.investigate(case, FakeGraph(), emit=broken))
    assert decisions(actual) == decisions(expected)
    assert "investigation_observer_failed" in caplog.text

    def mutating(_kind, payload):
        if "actions" in payload:
            payload["actions"].clear()
        if "answer" in payload:
            payload["answer"]["case"]["verdict"] = "fraud"
        payload.clear()

    actual, _ = asyncio.run(module.investigate(case, FakeGraph(), emit=mutating))
    assert decisions(actual) == decisions(expected)


def test_graph_failure_emits_error_and_preserves_exception(investigation):
    module, case = investigation
    recorder = EventRecorder(case["case_id"])
    with pytest.raises(RuntimeError, match="synthetic graph failure"):
        asyncio.run(module.investigate(case, FakeGraph(fail="card_baseline"), emit=recorder.emit))
    assert recorder.events[-1]["event_type"] == "error"
    assert not any(event["event_type"] == "done" for event in recorder.events)
    failed = recorder.events[-2]["payload"]
    assert failed["tool"] == "card_baseline" and failed["success"] is False
    assert failed["rows"] is None and failed["duration_ms"] >= 0


def test_row_counts_avoid_aliases_and_argument_summary_excludes_payloads():
    assert result_row_counts({"hits": [{"id": 1}], "cases": [{"id": 1}]}) == (1, {"hits": 1})
    assert result_row_counts({"transactions": [], "cases": [], "direct_cases": [{"id": 1}]})[0] == 0
    assert result_row_counts({"success_count": 3}) == (3, {"successful_writes": 3})
    assert result_row_counts({"ok": True}) == (None, {})
    assert args_summary({"card_id": "C1", "answer_json": "private text", "api_key": "secret",
                         "query_vec": [1, 2], "summary": "private text"}) == {"card_id": "C1", "query_vec_count": 2}


def test_upsert_observation_preserves_result_and_arguments():
    class Graph:
        use_local = False

        async def upsert_vectors(self, vertex_type, vectors, *, attribute):
            assert vertex_type == "FraudCase" and attribute == "emb_bge"
            assert vectors[0]["vector"] == [1., 0.]
            return {"success_count": 1}

    recorder = EventRecorder("HHG-TEST")
    observed = ObservedGraph(Graph(), safe_emitter(recorder.emit))
    assert asyncio.run(observed.upsert_vectors("FraudCase", [{"vector": [1., 0.]}], attribute="emb_bge")) == {"success_count": 1}
    payload = recorder.events[0]["payload"]
    assert payload["rows"] == 1 and payload["backend"] == "tigergraph-mcp"
    assert payload["args_summary"] == {"vertex_type": "FraudCase", "attribute": "emb_bge", "vectors_count": 1}


def test_atomic_timeline_and_recorder_copy(tmp_path, monkeypatch):
    recorder = EventRecorder("HHG-TEST")
    payload = {"actions": [{"action": "CREATE_CASE"}]}
    recorder.emit("nba_initial", payload)
    payload["actions"].clear()
    path = tmp_path / "timelines" / "HHG-TEST.json"
    write_timeline(path, recorder.events)
    original = path.read_bytes()
    assert read_timeline(path)[0]["payload"]["actions"] == [{"action": "CREATE_CASE"}]

    def fail_replace(*_args):
        raise OSError("synthetic disk failure")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="synthetic disk failure"):
        write_timeline(path, [])
    assert path.read_bytes() == original
    assert list(path.parent.glob("*.tmp")) == []
    with pytest.raises(ValueError, match="Unknown investigation event"):
        recorder.emit("fabricated", {})


def test_reconstruction_is_disclosed_and_does_not_invent_tool_timings(investigation):
    module, case = investigation
    answer, trace = asyncio.run(module.investigate(case, FakeGraph()))
    snapshot = json.dumps(answer, sort_keys=True)
    events = reconstruct_timeline(answer, trace, case)
    assert json.dumps(answer, sort_keys=True) == snapshot
    assert all(event["source"] == "reconstructed" and event["timestamp"] is None for event in events)
    assert "Reconstructed replay" in events[0]["payload"]["disclosure"]
    calls = [event["payload"] for event in events if event["event_type"] == "tool_call"]
    assert calls and all(item["duration_ms"] is None and item["rows"] is None and item["execution_observed"] is False for item in calls)
    assert not any(item["tool"] == "similar_cases" for item in calls)  # Never infer unrecorded calls.
    initial = next(event["payload"] for event in events if event["event_type"] == "assessment")
    assert initial["probability"] == trace["initial_probability"]
    assert initial["family_contributions"] == trace["initial_families"]
    assert events[-1]["payload"]["answer"] == answer
    assert next(event["payload"] for event in events if event["event_type"] == "case_written")["historical_writeback"] is True


def test_reconstruction_rejects_unrelated_trace_and_leaves_initial_unknown(investigation):
    module, case = investigation
    answer, _ = asyncio.run(module.investigate(case, FakeGraph()))
    events = reconstruct_timeline(answer, {"case_id": "OTHER", "initial_probability": .99})
    assert events[0]["payload"]["trace_matches_answer"] is False
    initial = next(event["payload"] for event in events if event["event_type"] == "assessment")
    assert initial["probability"] is None and initial["verdict"] is None
    assert all(event["elapsed_ms"] is None for event in events)
