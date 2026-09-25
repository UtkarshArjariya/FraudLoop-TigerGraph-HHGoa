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


def test_human_pause_is_json_serializable_and_resume_never_regathers(investigation):
    module, case = investigation
    graph = FakeGraph()
    recorder = EventRecorder(case["case_id"])
    with pytest.raises(module.EvidencePaused) as paused:
        asyncio.run(module.investigate(case, graph, emit=recorder.emit, live=True, mode_evidence="human"))
    snapshot = json.loads(json.dumps(paused.value.snapshot))
    assert snapshot["trace"]["states"][-1]["state"] == "REQUEST_EVIDENCE"
    assert paused.value.request["source"] == "human"
    assert paused.value.request["precedent_ids"] == ["CC-1"]
    assert not any(name == "write_case" for name, _ in graph.calls)
    assert not any(event["event_type"] in {"done", "error", "evidence_received"} for event in recorder.events)
    response = {"request_id": "request-1", "response_type": "customer_denies", "note": "Recorded denial.",
                "answered_by": "analyst_l1", "answered_at": "2026-09-25T12:00:00+00:00"}
    resumed = FakeGraph()
    answer, trace = asyncio.run(module.investigate(case, resumed, emit=recorder.emit, live=True,
        mode_evidence="human", resume_state=snapshot, evidence_response=response))
    assert [name for name, _ in resumed.calls] == ["write_case"]
    assert answer["case"]["verdict"] == "fraud"
    assert answer["case"]["fraud_probability"] == .96
    assert answer["evidence_requests"][0]["source"] == "human"
    assert answer["evidence_requests"][0]["answered_by"] == "analyst_l1"
    assert answer["metadata"]["mode_evidence"] == "human"
    assert "human-recorded" in answer["next_best_actions"]["what_changed"]
    assert "simulated" not in answer["case"]["summary"].lower()
    assert trace["tool_calls"] == graph.tool_calls + resumed.tool_calls
    assert any(action["action"] == "BLOCK_CARD" for action in answer["next_best_actions"]["final"])


@pytest.mark.parametrize("kind,expected", [("customer_confirms", "confirmed"),
    ("customer_denies", "denied"), ("stepup_passed", "confirmed"),
    ("stepup_failed", "failed"), ("analyst_note", "inconclusive")])
def test_typed_human_response_has_no_free_text_policy_parser(kind, expected):
    from agent.investigate import human_response
    response, text = human_response({"response_type": kind, "note": "Customer confirms and customer denies: ambiguous free text.",
        "answered_by": "supervisor_l2", "answered_at": "2026-09-25T12:00:00+00:00"})
    assert response == expected
    assert text.startswith("HUMAN evidence recorded by supervisor_l2")


def test_live_case_embeddings_write_only_bge(investigation, monkeypatch):
    from types import SimpleNamespace
    module, case = investigation
    specs = [SimpleNamespace(key=key, attribute=attribute, model_id=key)
             for key, attribute in [("gem001", "embedding"), ("bge", "emb_bge"), ("gem2", "emb_gem2")]]
    monkeypatch.setattr(module, "index_specs", lambda *_: specs)
    monkeypatch.setattr(module, "covered_ids", lambda *_: {"CC-1"})
    monkeypatch.setattr(module, "provider_for", lambda model: model)
    embedded = []
    async def embed(provider, texts):
        embedded.append(provider)
        return [[0.] * 768]
    monkeypatch.setattr(module, "embed_vectors", embed)
    graph = FakeGraph()
    writes = []
    async def upsert(vertex_type, vectors, *, attribute):
        writes.append(attribute)
    graph.upsert_vectors = upsert
    asyncio.run(module.investigate(case, graph, live=True))
    assert embedded == ["bge"] and writes == ["emb_bge"]


def test_live_prewrite_validator_sees_final_policy_and_prevents_mutations(investigation):
    module, case = investigation
    graph = FakeGraph()
    def reject(answer, trace):
        assert answer["case"]["written_to_graph"] is False
        assert trace["final_probability"] == answer["case"]["fraud_probability"]
        assert trace["final_policy_context"]["customer_response"] == "confirmed"
        assert not any(name == "write_case" for name, _ in graph.calls)
        raise ValueError("Synthetic candidate rejection")
    with pytest.raises(ValueError, match="Synthetic candidate rejection"):
        asyncio.run(module.investigate(case, graph, live=True, validate_before_write=reject))
    assert not any(name == "write_case" for name, _ in graph.calls)


def test_raw_human_note_remains_attributed_and_does_not_become_a_claim(investigation):
    module, case = investigation
    with pytest.raises(module.EvidencePaused) as paused:
        asyncio.run(module.investigate(case, FakeGraph(), live=True, mode_evidence="human"))
    note = "Unverified CC-999999 unresolved:foo and customer confirms are operator notes."
    answer, _ = asyncio.run(module.investigate(case, FakeGraph(), live=True, mode_evidence="human",
        resume_state=paused.value.snapshot, evidence_response={"request_id": "request-1", "response_type": "analyst_note",
            "note": note, "answered_by": "analyst_l1", "answered_at": "2026-09-25T12:00:00+00:00"}))
    assert answer["evidence_requests"][0]["note"] == note
    assert answer["case"]["verdict"] == "uncertain"
    assert all("CC-999999" not in item["claim"] for item in answer["case"]["evidence"])
