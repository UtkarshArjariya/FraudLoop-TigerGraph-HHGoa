"""Pause/restart boundaries preserve graded files and exactly-once human replies."""
import asyncio
from copy import deepcopy

import httpx
import pytest

from agent.investigate import EvidencePaused
from backend.data import atomic_json, read_json
from backend.evidence import PendingEvidence
from backend.services import InvestigationManager
from backend.tests.test_api import app  # shared isolated supplied-dataset fixture


async def eventually(predicate):
    async def poll():
        while not predicate():
            await asyncio.sleep(.001)
    await asyncio.wait_for(poll(), 2)


def paused_runner(calls):
    async def run(repo, gateway, case_id, emit, *, mode_evidence, resume_state=None, evidence_response=None):
        calls.append((mode_evidence, resume_state, evidence_response))
        if resume_state is None:
            emit("nba_initial", {"actions": [{"action": "VERIFY_WITH_CUSTOMER", "route": "auto"}]})
            raise EvidencePaused({"saved": "assessment"}, {"type": "customer_validation", "source": "human",
                "simulated": False, "reason": "Resolve authorization", "precedent_ids": ["CC-2649"]})
        assert resume_state == {"saved": "assessment"}
        assert evidence_response["response_type"] == "customer_denies"
        emit("evidence_received", {**evidence_response, "simulated": False})
        emit("nba_final", {"actions": [{"action": "BLOCK_CARD", "route": "L1"}], "what_changed": "Human denial"})
        answer = deepcopy(repo.answer(case_id))
        answer["metadata"] = {"mode_evidence": "human"}
        return answer
    return run


@pytest.mark.asyncio
async def test_pause_restart_response_reconnect_and_protected_outputs(app):
    repo, manager = app.state.repository, app.state.manager
    original_answer = (repo.cases / "HHG-014.json").read_bytes()
    original_timeline = (repo.data / "timelines/HHG-014.json").read_bytes()
    calls = []
    manager.live_runner = paused_runner(calls)
    run = await manager.start("HHG-014", "live", "human")
    await eventually(lambda: run.paused)
    pending = manager.evidence.public("HHG-014")
    assert pending["status"] == "pending"
    assert "snapshot" not in pending
    assert manager.evidence.get("HHG-014")["snapshot"] == {"saved": "assessment"}
    assert not app.state.gateway.lock.locked()
    assert not (repo.runtime / "HHG-014.json").exists()
    await manager.close()

    # Recreate the manager as after a process restart, without a live coroutine.
    restarted = InvestigationManager(repo, app.state.gateway)
    restarted.live_runner = paused_runner(calls)
    await restarted.restore_pending()
    resumed = restarted.runs["HHG-014"]
    assert resumed.run_id == run.run_id
    assert len(calls) == 1
    response = await restarted.submit_evidence("HHG-014", pending["request_id"], "customer_denies", "Recorded callback", "analyst_l1")
    await asyncio.wait_for(resumed.task, 2)
    assert len(calls) == 2
    assert response["response"]["answered_by"] == "analyst_l1"
    assert restarted.evidence.get("HHG-014")["status"] == "completed"
    assert restarted.evidence.public("HHG-014") is None
    assert read_json(repo.runtime / "HHG-014.json")["run_id"] == run.run_id
    assert read_json(repo.live_timelines / "HHG-014.json")["status"] == "completed"
    attached = await restarted.start("HHG-014", "live", run_id=run.run_id)
    assert attached is resumed and attached.finished
    assert (repo.cases / "HHG-014.json").read_bytes() == original_answer
    assert (repo.data / "timelines/HHG-014.json").read_bytes() == original_timeline
    # A second restart can attach the completed run without launching work.
    second = InvestigationManager(repo, app.state.gateway)
    attached = await second.start("HHG-014", "live", run_id=run.run_id)
    assert attached.finished and attached.events[-1]["event_type"] == "done"


@pytest.mark.asyncio
async def test_role_gate_invalid_notes_idempotency_and_case_detail(app):
    manager = app.state.manager
    calls = []
    manager.live_runner = paused_runner(calls)
    run = await manager.start("HHG-014", "live", "human")
    await eventually(lambda: run.paused)
    pending = manager.evidence.public("HHG-014")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        detail = (await client.get("/api/cases/HHG-014")).json()
        assert detail["status"] == "awaiting_evidence"
        assert detail["pending_evidence"]["request_id"] == pending["request_id"]
        body = {"request_id": pending["request_id"], "response_type": "customer_denies", "note": "Recorded callback"}
        assert (await client.post("/api/cases/HHG-014/evidence", json=body)).status_code == 403
        headers = {"X-Role": "analyst_l1"}
        assert (await client.post("/api/cases/HHG-014/evidence", json={**body, "response_type": "analyst_note", "note": " "}, headers=headers)).status_code == 422
        assert (await client.post("/api/cases/HHG-014/evidence", json={**body, "request_id": "stale"}, headers=headers)).status_code == 409
        result = await client.post("/api/cases/HHG-014/evidence", json=body, headers=headers)
        assert result.status_code == 200
        value = result.json()
        assert f"run_id={run.run_id}" in value["stream_url"]
        await asyncio.wait_for(run.task, 2)
        same = await client.post("/api/cases/HHG-014/evidence", json=body, headers=headers)
        assert same.status_code == 200 and same.json()["response"]["answered_at"] == value["response"]["answered_at"]
        changed = await client.post("/api/cases/HHG-014/evidence", json={**body, "response_type": "customer_confirms"}, headers=headers)
        assert changed.status_code == 409
        stream = await client.post(value["stream_url"])
        assert "event: done" in stream.text and "event: evidence_received" in stream.text
        assert len(calls) == 2


@pytest.mark.asyncio
async def test_crash_after_answer_promotion_never_reexecutes(app):
    repo = app.state.repository
    manager = app.state.manager
    record = manager.evidence.create("HHG-014", "run-crash", {"saved": "assessment"}, {"source": "human"})
    manager.evidence.submit("HHG-014", record["request_id"], "customer_denies", "", "analyst_l1")
    atomic_json(repo.runtime / "HHG-014.json", {"answer": repo.answer("HHG-014"), "run_id": "run-crash",
        "trace": {}, "timeline": [{"event_type": "done", "case_id": "HHG-014", "sequence": 2, "payload": {}}]})
    async def forbidden(*args, **kwargs):
        pytest.fail("Durable completed run must not be executed again")
    manager.live_runner = forbidden
    await manager.restore_pending()
    assert manager.evidence.get("HHG-014")["status"] == "completed"
    assert not manager.active


def test_pending_write_failure_never_signals_accepted_response(app, monkeypatch):
    repository = app.state.repository
    pending = PendingEvidence(repository)
    record = pending.create("HHG-014", "run-1", {}, {"source": "human"})
    def fail(*args):
        raise OSError("disk unavailable")
    monkeypatch.setattr("backend.evidence.atomic_json", fail)
    with pytest.raises(OSError):
        pending.submit("HHG-014", record["request_id"], "customer_denies", "", "analyst_l1")
    assert pending.get("HHG-014")["response"] is None
    assert not pending.signals[record["request_id"]].is_set()
