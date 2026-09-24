from __future__ import annotations

import asyncio
import csv
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from backend.app import create_app
from backend.data import atomic_json, epoch
from backend.services import GraphGateway, GuardedGraph, InvestigationManager, Run


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAUDLOOP_LOCAL_ONLY", "1")
    monkeypatch.setenv("FRAUDLOOP_REPLAY_DELAY", "0")
    monkeypatch.setenv("FRAUDLOOP_ENABLE_LLM", "0")
    (tmp_path / "HHGOA_IEEE").mkdir()
    (tmp_path / "data/graph").mkdir(parents=True)
    rows = [{"case_id": "HHG-014", "opened_at": "2016-12-01 00:00:00", "trigger_type": "risk_score",
        "trigger_text": "Review the recorded signal.", "flagged_txn_id": "3478561", "card_id": "C13487-K1", "customer_id": "C13487", "risk_score": "0.05"}]
    write_csv(tmp_path / "HHGOA_IEEE/case_pack.csv", rows)
    transactions = [{"txn_id": "3478561", "card_id": "C13487-K1", "customer_id": "C13487", "epoch": epoch(rows[0]["opened_at"]),
        "ts": rows[0]["opened_at"], "amount": "74.96", "channel": "online", "region": "191.0", "device_profile": "device|OS|browser|screen", "risk_score": "0.05"},
        {"txn_id": "3478562", "card_id": "C13488-K1", "customer_id": "C13488", "epoch": epoch(rows[0]["opened_at"]) - 60,
        "ts": "2016-11-30 23:59:00", "amount": "30", "channel": "online", "region": "191.0", "device_profile": "device|OS|browser|screen", "risk_score": "0.15"}]
    write_csv(tmp_path / "data/graph/transactions.csv", transactions)
    write_csv(tmp_path / "HHGOA_IEEE/closed_cases_history.csv", [{"case_id": "CC-2649", "closed_at": "2016-11-01 00:00:00", "outcome": "confirmed_fraud",
        "card_id": "C13488-K1", "pattern": "undocumented", "analyst_notes": "A prior recorded outcome."}])
    answer = {"case_id": "HHG-014", "case": {"status": "escalated", "verdict": "fraud", "fraud_probability": .91, "pattern": "undocumented",
        "affected_txn_ids": ["3478561"], "connected_device_profiles": ["device|OS|browser|screen"], "similar_prior_cases": ["CC-2649"],
        "evidence": [], "exposure_usd": 74.96}, "sar": {"file": True}, "next_best_actions": {"initial": [], "final": [
        {"action": "BLOCK_CARD", "route": "L1", "reason": "R2 recommendation"}, {"action": "FILE_REPORT", "route": "L2", "reason": "R6 recommendation"}], "what_changed": "nothing"}}
    atomic_json(tmp_path / "cases/HHG-014.json", answer)
    atomic_json(tmp_path / "data/timelines/HHG-014.json", [
        {"event_type": "step_started", "sequence": 1, "case_id": "HHG-014", "source": "reconstructed", "payload": {"state": "GATHER"}},
        {"event_type": "done", "sequence": 2, "case_id": "HHG-014", "source": "reconstructed", "payload": {"case_id": "HHG-014"}}])
    return create_app(tmp_path)


def write_csv(path, rows):
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


@pytest.fixture
def client(app):
    with TestClient(app) as client:
        yield client


@pytest.mark.parametrize("path,key", [("/api/health", "tigergraph"), ("/api/cases", "cases"),
    ("/api/cases/HHG-014", "answer"), ("/api/cases/HHG-014/timeline", "events"),
    ("/api/cases/HHG-014/graph", "nodes"), ("/api/cases/HHG-014/precedents", "precedents"),
    ("/api/approvals", "pending"), ("/api/memory/stats", "closed_cases")])
def test_all_read_endpoints(client, path, key):
    response = client.get(path)
    assert response.status_code == 200
    assert key in response.json()


def test_queue_and_detail_contract(client):
    row = client.get("/api/cases").json()["cases"][0]
    assert row["pending_approval_count"] == 2
    assert row["probability"] == .91
    detail = client.get("/api/cases/HHG-014").json()
    assert detail["answer_source"] == "graded"
    assert detail["trigger"]["flagged_txn_id"] == "3478561"


def test_health_is_cached_state_without_models_or_secrets(client):
    health = client.get("/api/health").json()
    assert health["gemini"]["live_checked"] is False
    assert health["gemini"]["status"] == "template"
    assert "scope" not in json.dumps(health)
    assert health["demo_role_header"] is True


def test_replay_stream_without_live_runner(client, app):
    async def forbidden(*args):
        pytest.fail("Replay must not invoke investigation core")
    app.state.manager.live_runner = forbidden
    response = client.post("/api/cases/HHG-014/investigate?mode=replay")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: step_started" in response.text and "event: done" in response.text
    assert '"source": "reconstructed"' in response.text


def test_live_wrapper_stream_with_fake_runner(client, app):
    async def run(repo, gateway, cid, emit):
        emit("assessment", {"probability": .5, "verdict": "uncertain", "family_contributions": {}})
        return repo.answer(cid)
    app.state.manager.live_runner = run
    original = (app.state.repository.cases / "HHG-014.json").read_bytes()
    response = client.post("/api/cases/HHG-014/investigate?mode=live")
    assert response.status_code == 200 and "event: assessment" in response.text
    assert "event: done" in response.text
    assert (app.state.repository.cases / "HHG-014.json").read_bytes() == original
    assert app.state.repository.timeline("HHG-014")[-1]["event_type"] == "done"


def test_live_errors_are_clear_and_redacted(client, app):
    async def broken(*args):
        raise RuntimeError("api_key=secret-value private remote error")
    app.state.manager.live_runner = broken
    response = client.post("/api/cases/HHG-014/investigate?mode=live")
    assert "event: error" in response.text
    assert "RuntimeError" in response.text
    assert "secret-value" not in response.text


def test_core_done_is_not_published_before_wrapper_validation(client, app):
    async def invalid_after_done(repo, gateway, cid, emit):
        emit("done", {"case_id": cid})
        raise ValueError("Runtime answer failed validation")
    app.state.manager.live_runner = invalid_after_done
    response = client.post("/api/cases/HHG-014/investigate?mode=live")
    assert "event: error" in response.text and "event: done" not in response.text


@pytest.mark.parametrize("body", [{"type": "risk_signal", "txn_id": "3478561"},
    {"type": "customer_report", "card_id": "C13487-K1", "text": "I do not recognize this transaction."},
    {"type": "analyst_request", "card_id": "C13487-K1", "note": "Review the latest supplied transaction."}])
def test_new_trigger_real_ids_temporal_scope(client, app, body):
    response = client.post("/api/investigations", json=body)
    assert response.status_code == 201
    value = response.json()
    assert value["case_id"].startswith("CASE-")
    assert value["stream_method"] == "POST"
    assert value["trigger"]["opened_at"] == "2016-12-01 00:00:00"
    detail = client.get(f"/api/cases/{value['case_id']}").json()
    assert detail["answer"] is None and detail["status"] == "queued"
    assert not (app.state.repository.cases / f"{value['case_id']}.json").exists()


@pytest.mark.parametrize("body", [{"type": "risk_signal", "txn_id": "9999999"},
    {"type": "risk_signal"}, {"type": "customer_report", "card_id": "C13487-K1", "text": " "},
    {"type": "analyst_request", "card_id": "C99999-K1", "note": "Review"},
    {"type": "risk_signal", "txn_id": "../../.env"},
    {"type": "risk_signal", "txn_id": "3478561", "card_id": "C13487-K1"}])
def test_invalid_triggers(client, body):
    response = client.post("/api/investigations", json=body)
    assert response.status_code == 422 and "error" in response.json()


def test_unresolved_card_cannot_become_authoritative_trigger(client, app):
    write_csv(app.state.repository.data / "graph/transactions.csv", [{"txn_id": "123", "card_id": "unresolved:internal"}])
    assert client.post("/api/investigations", json={"type": "risk_signal", "txn_id": "123"}).status_code == 422


@pytest.mark.parametrize("path", ["/api/cases/missing", "/api/cases/missing/timeline", "/api/cases/missing/graph", "/api/cases/missing/precedents"])
def test_case_404(client, path):
    response = client.get(path)
    assert response.status_code == 404 and response.json()["error"]["message"] == "Case not found."


def test_approval_permissions_and_durable_audit(client, app):
    pending = client.get("/api/approvals").json()["pending"]
    first, second = pending
    route = lambda row: "/api/approvals/" + row["id"]
    for role in ("viewer", "owner", "unknown"):
        assert client.post(route(first), headers={"X-Role": role}, json={"decision": "approve"}).status_code == 403
    assert client.post(route(second), headers={"X-Role": "analyst_l1"}, json={"decision": "approve"}).status_code == 403
    decision = client.post(route(first), headers={"X-Role": "analyst_l1"}, json={"decision": "approve", "note": "Local demo approval"})
    assert decision.status_code == 200
    assert decision.json()["executed"] is False and decision.json()["written_to_graph"] is False
    assert client.post(route(first), headers={"X-Role": "analyst_l1"}, json={"decision": "approve"}).status_code == 409
    assert client.post(route(second), headers={"X-Role": "supervisor_l2"}, json={"decision": "reject", "note": "Review required"}).status_code == 200
    current = client.get("/api/approvals").json()
    assert len(current["audit"]) == 2 and current["pending"] == []
    assert len(json.loads(app.state.repository.approvals_path.read_text())["audit"]) == 2
    assert client.get("/api/cases").json()["cases"][0]["pending_approval_count"] == 0


def test_supervisor_can_decide_l1(client):
    first = client.get("/api/approvals").json()["pending"][0]
    response = client.post("/api/approvals/" + first["id"], headers={"X-Role": "supervisor_l2"}, json={"decision": "reject"})
    assert response.status_code == 200


def test_new_recommendation_invalidates_previous_approval(client, app):
    first = client.get("/api/approvals").json()["pending"][0]
    client.post("/api/approvals/" + first["id"], headers={"X-Role": "analyst_l1"}, json={"decision": "approve"})
    answer = app.state.repository.answer("HHG-014")
    answer["next_best_actions"]["final"][0]["reason"] = "Changed recommendation"
    atomic_json(app.state.repository.runtime / "HHG-014.json", answer)
    assert len(client.get("/api/approvals").json()["pending"]) == 2


def test_changed_evidence_invalidates_approval_even_with_same_actions(client, app):
    first = client.get("/api/approvals").json()["pending"][0]
    client.post("/api/approvals/" + first["id"], headers={"X-Role": "analyst_l1"}, json={"decision": "approve"})
    answer = app.state.repository.answer("HHG-014")
    answer["case"]["fraud_probability"] = .75
    atomic_json(app.state.repository.runtime / "HHG-014.json", answer)
    assert len(client.get("/api/approvals").json()["pending"]) == 2


def test_graph_flags_temporal_and_truthful_fallback(client, app):
    graph = client.get("/api/cases/HHG-014/graph").json()
    assert graph["source"] == "local" and graph["written_to_graph"] is False
    nodes = {row["data"]["id"]: row["data"] for row in graph["nodes"]}
    assert nodes["Card:C13487-K1"]["flagged"]
    assert nodes["Card:C13488-K1"]["fraud_history"]
    assert len(nodes) <= 150
    assert all(edge["data"]["source"] in nodes and edge["data"]["target"] in nodes for edge in graph["edges"])


def test_cors(client):
    response = client.options("/api/cases", headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET"})
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_structured_http_logging_is_visible_once_and_excludes_request_contents(client):
    from io import StringIO
    from backend.app import configure_logging, logger

    configure_logging()
    configure_logging()
    handlers = [handler for handler in logger.handlers if getattr(handler, "fraudloop_json", False)]
    assert len(handlers) == 1 and logger.propagate is False
    stream = StringIO()
    previous_stream = handlers[0].setStream(stream)
    try:
        client.get("/api/cases/HHG-014?api_key=secret-query", headers={"Authorization": "Bearer secret-header"})
        client.post("/api/investigations", json={"type": "customer_report", "card_id": "C13487-K1", "text": "private-narrative"})
        client.get("/unmatched-secret-path")
    finally:
        handlers[0].setStream(previous_stream)
    output = stream.getvalue()
    records = [json.loads(line) for line in output.splitlines()]
    assert len(records) == 3 and all(row["event"] == "http_request" for row in records)
    assert [row["path"] for row in records] == ["/api/cases/{case_id}", "/api/investigations", "unmatched"]
    assert [row["status"] for row in records] == [200, 201, 404]
    assert all(key not in output for key in ("secret-query", "secret-header", "private-narrative", "unmatched-secret-path"))


@pytest.mark.asyncio
async def test_disconnect_does_not_cancel_background_run(app):
    manager = app.state.manager
    release = asyncio.Event()
    async def slow(repo, gateway, cid, emit):
        emit("step_started", {"state": "GATHER"})
        await release.wait()
        return repo.answer(cid)
    manager.live_runner = slow
    run = await manager.start("HHG-014", "live")
    stream = manager.stream(run)
    await anext(stream)
    await stream.aclose()
    assert not run.finished
    release.set()
    await run.task
    assert run.finished and run.events[-1]["event_type"] == "done"


@pytest.mark.asyncio
async def test_only_one_live_run_and_active_mode_conflict(app):
    manager = app.state.manager
    hold = asyncio.Event()
    async def slow(*args):
        await hold.wait()
        return {}
    manager.live_runner = slow
    run = await manager.start("HHG-014", "live")
    with pytest.raises(ValueError):
        await manager.start("HHG-014", "replay")
    with pytest.raises(ValueError):
        await manager.start("CASE-new", "live")
    assert await manager.start("HHG-014", "live") is run
    hold.set()
    await run.task


@pytest.mark.asyncio
async def test_gse_write_guard_restarts_once(app, monkeypatch):
    gateway = app.state.gateway
    gateway.local_only = False
    calls, sleeps = [], []
    async def command(value):
        calls.append(value)
        return "down" if len(calls) == 1 else "up"
    async def sleep(seconds):
        sleeps.append(seconds)
    monkeypatch.setattr("backend.services.docker_gadmin", command)
    monkeypatch.setattr("backend.services.asyncio.sleep", sleep)
    assert await gateway.gse_for_write() is True
    assert calls == ["gadmin status gse", "gadmin restart gse -y", "gadmin status gse"]
    assert sleeps == [30]


@pytest.mark.asyncio
async def test_gse_still_down_skips_write(app, monkeypatch):
    gateway = app.state.gateway
    gateway.local_only = False
    calls = []
    async def command(value):
        calls.append(value)
        return "down"
    async def sleep(seconds):
        pass
    monkeypatch.setattr("backend.services.docker_gadmin", command)
    monkeypatch.setattr("backend.services.asyncio.sleep", sleep)
    result = await gateway.audit_approval({"case_id": "HHG-014"})
    assert result["written_to_graph"] is False and len(calls) == 3


@pytest.mark.asyncio
async def test_graph_audit_preserves_saved_graph_payload(app, monkeypatch):
    from contextlib import asynccontextmanager
    gateway = app.state.gateway
    gateway.local_only = False
    original = {"case_id": "HHG-014", "case": {"verdict": "fraud"}, "custom": {"keep": True}}
    calls = []
    class Graph:
        graph_name = "FraudGraph"
        async def tool(self, name, arguments):
            calls.append((name, arguments))
            return {"attributes": {"answer_json": json.dumps(original), "embedding": [1, 2, 3]}} if name == "get_node" else {"success": True}
    @asynccontextmanager
    async def client():
        yield Graph()
    async def ready():
        return True
    monkeypatch.setattr(gateway, "client", client)
    monkeypatch.setattr(gateway, "gse_for_write", ready)
    result = await gateway.audit_approval({"case_id": "HHG-014", "audit_id": "test", "decision": "approve"})
    assert result["written_to_graph"] is True
    written = calls[-1][1]["attributes"]
    assert list(written) == ["answer_json"]
    payload = json.loads(written["answer_json"])
    assert payload["case"] == original["case"] and payload["custom"] == original["custom"]
    assert payload["approval_audit"][0]["audit_id"] == "test"


@pytest.mark.asyncio
async def test_each_write_checks_gse(app):
    checks, writes = [], []
    class Graph:
        use_local = False
        graph_name = "FraudGraph"
        async def tool(self, name, params):
            return {}
        async def call(self, name, params):
            writes.append(name)
            return {}
        async def upsert_vectors(self, *args, **kwargs):
            writes.append("upsert_vectors")
            return {}
    async def ready():
        checks.append(True)
        return True
    app.state.gateway.gse_for_write = ready
    graph = GuardedGraph(Graph(), app.state.gateway, None)
    await graph.call("write_case", {"case_id": "HHG-014", "answer_json": "{}"})
    await graph.upsert_vectors("FraudCase", [])
    assert writes == ["write_case", "upsert_vectors"] and len(checks) == 3


def test_failed_live_attempt_preserves_successful_replay(client, app):
    saved = app.state.repository.answer("HHG-014")
    async def successful(repo, gateway, cid, emit):
        emit("step_started", {"state": "GATHER"})
        return saved
    app.state.manager.live_runner = successful
    assert "event: done" in client.post("/api/cases/HHG-014/investigate?mode=live").text
    before_answer = app.state.repository.answer("HHG-014")
    before_timeline = app.state.repository.timeline("HHG-014")
    async def broken(*args):
        raise RuntimeError("Failed attempt")
    app.state.manager.live_runner = broken
    assert "event: error" in client.post("/api/cases/HHG-014/investigate?mode=live").text
    assert app.state.repository.answer("HHG-014") == before_answer
    assert app.state.repository.timeline("HHG-014") == before_timeline
    replay = client.post("/api/cases/HHG-014/investigate?mode=replay")
    assert "event: done" in replay.text and "event: error" not in replay.text
    assert list((app.state.repository.data / "runtime_failed_timelines").glob("HHG-014-*.json"))


def test_snapshot_failure_never_publishes_done_or_replaces_previous_answer(client, app, monkeypatch):
    before = app.state.repository.answer("HHG-014")
    async def successful(repo, gateway, cid, emit):
        return {**before, "revision": "new"}
    app.state.manager.live_runner = successful
    actual = atomic_json
    def disk_full(path, value):
        if path.parent == app.state.repository.runtime:
            raise OSError("Disk full")
        actual(path, value)
    monkeypatch.setattr("backend.services.atomic_json", disk_full)
    response = client.post("/api/cases/HHG-014/investigate?mode=live")
    assert "event: error" in response.text and "event: done" not in response.text
    assert app.state.repository.answer("HHG-014") == before


@pytest.mark.asyncio
async def test_live_case_write_preserves_graph_and_local_approval_audit(app):
    graph_audit = {"audit_id": "graph-only", "case_id": "HHG-014", "decision": "reject"}
    local_audit = {"audit_id": "local-only", "case_id": "HHG-014", "decision": "approve"}
    atomic_json(app.state.repository.approvals_path, {"audit": [local_audit]})
    captured = []
    class Graph:
        use_local = False
        graph_name = "FraudGraph"
        async def tool(self, name, params):
            return {"attributes": {"answer_json": json.dumps({"approval_audit": [graph_audit]})}}
        async def call(self, name, params):
            captured.append(params)
            return {}
    async def ready():
        return True
    app.state.gateway.gse_for_write = ready
    graph = GuardedGraph(Graph(), app.state.gateway, None)
    fresh = {"case_id": "HHG-014", "case": {"verdict": "uncertain"}}
    await graph.call("write_case", {"case_id": "HHG-014", "answer_json": json.dumps(fresh)})
    saved = json.loads(captured[0]["answer_json"])
    assert saved["case"] == fresh["case"]
    assert {row["audit_id"] for row in saved["approval_audit"]} == {"local-only", "graph-only"}


@pytest.mark.asyncio
async def test_all_runtime_ids_registered_and_only_earlier_memory_seeded(app):
    import pandas as pd
    from agent.answer import _index
    from agent.local_backend import LocalBackend
    from agent.store import TransactionStore
    from backend.services import prepare_runtime_store

    repo = app.state.repository
    earlier = repo.new_trigger({"type": "risk_signal", "txn_id": "3478562"})
    current = repo.new_trigger({"type": "risk_signal", "txn_id": "3478561"})
    atomic_json(repo.triggers_path, {row["case_id"]: row for row in (earlier, current)})
    # Reuse a schema-valid answer fixture. This test concerns persisted runtime
    # identities and memory cutoffs, not the separately tested evidence policy.
    answer = json.loads((Path(__file__).resolve().parents[2] / "cases/HHG-001.json").read_text())
    answer["case"].update(written_to_graph=False, graph_case_id="")
    for trigger in (earlier, current):
        atomic_json(repo.runtime / f"{trigger['case_id']}.json", {**answer, "case_id": trigger["case_id"]})
    transactions = pd.read_csv(repo.data / "graph/transactions.csv", dtype=str).fillna("")
    for column in ("p_email", "r_email", "card_key"):
        transactions[column] = ""
    history = pd.DataFrame(columns=["case_id", "card_id", "customer_id", "connected_card_ids", "closed_at", "txn_ids"])
    pack = pd.read_csv(repo.root / "HHGOA_IEEE/case_pack.csv", dtype=str).fillna("")
    store = TransactionStore(transactions, history, pack, {})
    prepare_runtime_store(repo, store, current["case_id"])
    known = _index(store)
    assert {earlier["case_id"], current["case_id"]} <= known["known"]
    assert {earlier["case_id"], current["case_id"]} <= set(known["pack"])
    memory = (await LocalBackend(store).call("case_library", {"as_of": epoch(current["opened_at"])}))["memory"]
    assert [row["case_id"] for row in memory] == [earlier["case_id"]]
    assert current["case_id"] not in store.case_memory
