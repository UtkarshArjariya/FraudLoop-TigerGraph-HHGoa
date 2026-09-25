"""Temporal detector evidence, bounded queues, lifecycle and MCP-only ingestion."""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import json
import time

from fastapi import FastAPI
import httpx
import pandas as pd
import pytest

from backend.data import atomic_json, epoch, read_json
from backend.detector import CONTEXT_COLUMNS, TXN_COLUMNS, DetectorConfig, DetectorSettings, PreparedSource, score_transaction
from backend.services import Run
from backend.stream import MAX_PENDING, StreamManager, StreamStart, graph_operations, stream_router, upsert_transaction
from backend.tests.test_api import app, write_csv


def transaction(txn_id="3478561", *, at="2016-12-01 00:00:00", **values):
    row = dict.fromkeys(TXN_COLUMNS, "")
    row.update(txn_id=txn_id, epoch=epoch(at), ts=at, txn_seconds="10", amount=74.96,
        card_id="C13487-K1", card_key="C13487-K1", customer_id="C13487", product="C", channel="online",
        risk_score=".8", device_profile="device|OS|browser|screen", region="191.0", p_email="example.org", r_email="example.org")
    row.update(values)
    return row


def prepared(rows=None, history=None):
    frame = pd.DataFrame(rows or [transaction()]).sort_values(["epoch", "txn_id"]).reset_index(drop=True)
    return PreparedSource(frame, frame.loc[:, list(CONTEXT_COLUMNS)], history or [],
                          "2016-10-01T00:00:00+00:00", "2016-12-01T00:00:00+00:00")


@pytest.fixture
def stream(app):
    value = StreamManager(app.state.repository, app.state.gateway, app.state.manager, asyncio.Lock())
    value.source = prepared()
    return value


async def eventually(predicate, timeout=3):
    async def poll():
        while not predicate():
            await asyncio.sleep(.005)
    await asyncio.wait_for(poll(), timeout)


def test_chunked_prepared_source_only_final_two_months_but_prior_context_survives(app, monkeypatch):
    repo = app.state.repository
    rows = [transaction("3478562", at="2016-12-01 00:00:00"),
            transaction("3478560", at="2016-08-01 00:00:00"),
            transaction("3478561", at="2016-11-01 00:00:00")]
    for row in rows:
        row["forbidden_original_label"] = "must-not-load"
    write_csv(repo.data / "graph/transactions.csv", rows)
    calls, original = [], pd.read_csv
    def observed(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)
    monkeypatch.setattr(pd, "read_csv", observed)
    source = PreparedSource.load(repo, chunk_size=1)
    assert source.rows.txn_id.tolist() == ["3478561", "3478562"]
    assert len(source.context) == 3
    assert "forbidden_original_label" not in source.rows
    assert all(call.get("chunksize") == 1 and "usecols" in call for call in calls)
    assert source.offset("2016-11-01 00:00:00")[0] == 0
    assert source.offset("2016-12-01T00:00:00Z")[0] == 1
    with pytest.raises(ValueError, match="final two months"):
        source.offset("2016-08-01")


def test_detector_risk_and_common_device_do_not_establish_fraud():
    row = transaction()
    result = score_transaction(row, {"window": [row], "baseline": [], "ring": [row], "cases": []}, DetectorConfig())
    assert result["risk_band"] == "high" and result["alert_score"] == .25
    assert not result["alert"]
    assert result["checks"]["confirmed_case_ids"] == []


def test_device_fraud_requires_complete_profile_other_card_and_closed_within_seven_days():
    row = transaction(risk_score="0")
    other = transaction("3478559", at="2016-11-30 00:00:00", card_id="C13488-K1")
    def history(cid, closed, **kwargs):
        return {"case_id": cid, "closed_at": closed, "outcome": "confirmed_fraud", "card_id": "C13488-K1", "txn_ids": other["txn_id"], **kwargs}
    context = {"ring": [other], "cases": [history("CC-1", "2016-11-30 10:00:00"),
        history("CC-FUTURE", "2016-12-02 00:00:00"), history("CC-OLD", "2016-11-01 00:00:00"),
        history("CC-SAME", "2016-11-30 00:00:00", card_id=row["card_id"])]}
    result = score_transaction(row, context, DetectorConfig())
    assert result["checks"]["confirmed_case_ids"] == ["CC-1"]
    assert result["contributions"]["confirmed_device_history"] == .5
    incomplete = score_transaction({**row, "device_profile": "||browser|"}, context, DetectorConfig())
    assert incomplete["checks"]["confirmed_case_ids"] == []


def test_velocity_and_card_testing_share_one_capped_component_and_ignore_future():
    row = transaction(risk_score="0")
    before = [transaction(f"347855{i}", at=f"2016-11-30 23:5{i}:00", amount=1.) for i in range(5)]
    future = transaction("3478569", at="2016-12-02 00:00:00", amount=1.)
    result = score_transaction(row, {"window": [*before, future, before[0]], "baseline": []}, DetectorConfig())
    assert result["checks"]["velocity_count"] == 6
    assert result["checks"]["card_testing"]
    assert result["alert_score"] == .6
    assert result["contributions"]["card_behavior"] == .6
    simultaneous = [transaction(f"347855{i}", amount=1.) for i in range(5)]
    result = score_transaction(row, {"window": simultaneous}, DetectorConfig())
    assert result["checks"]["velocity_count"] == 6
    assert not result["checks"]["card_testing"]


def test_new_region_uses_only_prior_baseline_and_no_history_is_not_new_evidence():
    row = transaction(risk_score="0")
    previous = transaction("3478559", at="2016-11-30 00:00:00", region="200")
    future = transaction("3478569", at="2016-12-02 00:00:00", region=row["region"])
    result = score_transaction(row, {"baseline": [previous, future]}, DetectorConfig())
    assert result["checks"]["new_region"] and result["alert_score"] == .15
    assert not score_transaction(row, {"baseline": [future]}, DetectorConfig())["checks"]["new_region"]


@pytest.mark.asyncio
async def test_upserts_are_idempotent_mcp_calls_and_preserve_schema_types():
    class Graph:
        graph_name = "FraudGraph"
        def __init__(self):
            self.calls, self.vertices, self.edges = [], {}, {}
        async def tool(self, name, values):
            self.calls.append((name, deepcopy(values)))
            assert name in {"add_node", "add_edge"}
            if name == "add_node":
                self.vertices[(values["vertex_type"], values["vertex_id"])] = values["attributes"]
            else:
                self.edges[tuple(values[key] for key in ("source_vertex_type", "source_vertex_id", "edge_type", "target_vertex_type", "target_vertex_id"))] = values["attributes"]
    row, graph = transaction(), Graph()
    await upsert_transaction(graph, row)
    snapshot = deepcopy((graph.vertices, graph.edges))
    first_calls = deepcopy(graph.calls)
    await upsert_transaction(graph, row)
    assert (graph.vertices, graph.edges) == snapshot
    assert graph.calls == first_calls * 2
    attrs = graph.vertices[("Txn", row["txn_id"])]
    assert isinstance(attrs["epoch"], int) and isinstance(attrs["amount"], float)
    assert isinstance(attrs["C1"], str)
    assert ("Card", row["card_key"]) in graph.vertices
    assert {key[2] for key in graph.edges} == {"CardTxn", "Owns", "DeviceTxn", "CardDevice", "RegionTxn", "PurchaserEmail", "RecipientEmail"}
    nodes, edges = graph_operations(transaction(card_key="unresolved:real-key", card_id="", device_profile="|||"))
    assert any(kind == "Card" and identifier == "unresolved:real-key" for kind, identifier, _ in nodes)
    assert not any(kind == "DeviceProfile" for kind, _, _ in nodes)


@pytest.mark.asyncio
async def test_busy_healthy_graph_applies_backpressure_then_upserts(stream):
    stream.gateway.local_only = False
    writes = []
    class Graph:
        graph_name = "FraudGraph"
        async def tool(self, name, arguments):
            writes.append(name)
        async def call(self, name, arguments):
            return {"transactions": [transaction()], "cases": []}
    @asynccontextmanager
    async def client():
        yield Graph()
    async def guard():
        return True
    stream.gateway.client = client
    stream.gateway.gse_for_write = guard
    await stream.gateway.lock.acquire()
    task = asyncio.create_task(stream.process_transaction(transaction(), DetectorConfig()))
    await asyncio.sleep(.01)
    assert not task.done() and not writes
    stream.gateway.lock.release()
    _, graph = await asyncio.wait_for(task, 1)
    assert writes and graph["written_to_graph"] and graph["source"] == "tigergraph-mcp"


@pytest.mark.asyncio
async def test_graph_queries_use_transaction_cutoff_and_guard_precedes_mcp_upserts(stream):
    observed = []
    class Graph:
        graph_name = "FraudGraph"
        async def tool(self, name, arguments):
            assert observed[0] == "gse_guard"
            observed.append(name)
        async def call(self, name, arguments):
            observed.append((name, arguments))
            return {"transactions": [transaction()], "cases": []}
    @asynccontextmanager
    async def client():
        yield Graph()
    async def guard():
        observed.append("gse_guard")
        return True
    stream.gateway.local_only = False
    stream.gateway.client = client
    stream.gateway.gse_for_write = guard
    _, graph = await stream.process_transaction(transaction(), DetectorConfig(velocity_window_minutes=15))
    assert graph["written_to_graph"] and graph["source"] == "tigergraph-mcp"
    queries = [value for value in observed if isinstance(value, tuple)]
    assert [name for name, _ in queries] == ["card_window", "card_baseline", "device_ring"]
    assert all(values["as_of"] == transaction()["epoch"] for _, values in queries)
    assert queries[-1][1]["days"] == 7


@pytest.mark.asyncio
async def test_partial_graph_failure_is_disclosed_and_avoids_repeated_operations(stream):
    called = []
    class Graph:
        graph_name = "FraudGraph"
        async def tool(self, name, arguments):
            called.append(name)
            if len(called) == 2:
                raise RuntimeError("private connection details")
    @asynccontextmanager
    async def client():
        yield Graph()
    async def guard():
        return True
    stream.gateway.local_only = False
    stream.gateway.client = client
    stream.gateway.gse_for_write = guard
    _, graph = await stream.process_transaction(transaction(), DetectorConfig())
    assert not graph["written_to_graph"] and graph["partial_write_possible"]
    assert "private" not in graph["warning"]
    await stream.process_transaction(transaction(), DetectorConfig())
    assert called == ["add_node", "add_node"]


@pytest.mark.asyncio
async def test_pending_queue_is_capped_deduplicated_and_never_uses_unresolved_cards(stream):
    stream.investigations.runs["other"] = Run("other", "live", paused=True)
    for position in range(25):
        row = transaction(str(3478500 + position))
        await stream.open_case(row, score_transaction(row, {}, DetectorConfig(threshold=0)), {})
    assert len(stream.pending) == MAX_PENDING
    assert stream.counts["cases_opened"] == 10 and stream.counts["queue_rejections"] == 15
    await stream.open_case(transaction("3478500"), {"alert_score": 0, "reasons": []}, {})
    assert stream.counts["duplicate_alerts"] == 1
    await stream.open_case(transaction("3478999", card_id="", card_key="unresolved:x"), {"alert_score": 1}, {})
    assert stream.counts["unresolved_alerts"] == 1
    records = read_json(stream.repo.triggers_path)
    assert len(records) == 10
    assert all(value["trigger_type"] == "risk_signal" for value in records.values())
    assert all(value["trigger_source_type"] == "risk_signal" for value in records.values())
    assert all(value["opened_at"] == transaction()["ts"] for value in records.values())


@pytest.mark.asyncio
async def test_queue_serializes_investigations_and_stop_preserves_active_run(stream):
    releases = asyncio.Event()
    concurrent, maximum, completed = 0, 0, []
    async def runner(repo, gateway, cid, emit, **kwargs):
        nonlocal concurrent, maximum
        concurrent += 1
        maximum = max(maximum, concurrent)
        await releases.wait()
        answer = {"case_id": cid, "case": {"status": "closed_legitimate", "verdict": "legitimate", "written_to_graph": False}}
        concurrent -= 1
        completed.append(cid)
        return answer
    stream.investigations.live_runner = runner
    for number in range(2):
        row = transaction(str(3478561 + number))
        await stream.open_case(row, {"alert_score": .7, "reasons": ["Synthetic detector condition."]}, {})
    stream.worker = asyncio.create_task(stream.investigate_queue())
    await eventually(lambda: stream.active_case_id is not None)
    active = stream.investigations.runs[stream.active_case_id]
    await stream.stop()
    assert not active.task.done() and len(stream.pending) == 0
    releases.set()
    await eventually(lambda: stream.counts["investigations_done"] == 1)
    assert maximum == 1 and len(completed) == 1
    assert stream.cases[0]["status"] == "queued_manual"
    await stream.close()
    await stream.investigations.close()


@pytest.mark.asyncio
async def test_rate_cap_pause_resume_and_bounded_event_delivery(stream):
    stream.source = prepared([transaction(str(3478561 + i)) for i in range(3)])
    stream.settings.update({"threshold": 1})
    ticks = []
    original = stream.process_transaction
    async def process(*args):
        ticks.append(time.monotonic())
        return await original(*args)
    stream.process_transaction = process
    await stream.start(StreamStart(speed_multiplier=10))
    assert stream.target_tps == 5
    await eventually(lambda: len(ticks) == 1)
    await stream.pause()
    await asyncio.sleep(.25)
    assert len(ticks) == 1
    await stream.start(StreamStart(speed_multiplier=10))
    await eventually(lambda: stream.state == "completed")
    assert ticks[2] - ticks[1] >= .19
    assert stream.status()["transactions_ingested"] == 3
    viewer = asyncio.Queue(maxsize=100)
    stream.subscribers.add(viewer)
    for position in range(1000):
        stream.publish("txn_ingested", {"txn_id": str(position)})
    assert len(stream.events) == 500 and viewer.qsize() == 100
    stream.subscribers.discard(viewer)
    events = stream.event_stream()
    first = await anext(events)
    assert "event: stream_status" in first and '"state": "completed"' in first
    stream.publish("txn_ingested", {"txn_id": "last"})
    assert '"txn_id": "last"' in await anext(events)
    await events.aclose()
    await stream.close()


@pytest.mark.asyncio
async def test_stop_and_queue_claim_are_serialized(stream):
    blocked = asyncio.Event()
    entered = asyncio.Event()
    active = asyncio.Event()
    original_start = stream.investigations.start
    async def start(*args, **kwargs):
        entered.set()
        await blocked.wait()
        return await original_start(*args, **kwargs)
    async def runner(*args, **kwargs):
        await active.wait()
        return {"case": {"status": "open"}}
    stream.investigations.start = start
    stream.investigations.live_runner = runner
    await stream.open_case(transaction(), {"alert_score": .6, "reasons": []}, {})
    stream.worker = asyncio.create_task(stream.investigate_queue())
    await entered.wait()
    stopping = asyncio.create_task(stream.stop())
    await asyncio.sleep(.01)
    assert not stopping.done()  # The claimed case must become active first.
    blocked.set()
    await asyncio.wait_for(stopping, 1)
    assert stream.state == "stopped" and not stream.pending
    assert stream.active_case_id in stream.investigations.active
    active.set()
    await stream.close()
    await stream.investigations.close()


@pytest.mark.asyncio
async def test_sse_reconnect_replays_in_order_then_sends_current_snapshot(stream):
    for position in range(3):
        stream.publish("txn_ingested", {"txn_id": str(position)})
    stream.state = "paused"
    events = stream.event_stream(after=0)
    output = [await anext(events) for _ in range(3)]
    ids = [int(value.splitlines()[0].split(": ")[1]) for value in output]
    assert ids == [1, 2, 2]
    assert "event: stream_status" in output[-1] and '"state": "paused"' in output[-1]
    await events.aclose()


@pytest.mark.asyncio
async def test_api_roles_partial_config_validation_and_start_date(stream):
    host = FastAPI()
    host.include_router(stream_router(stream))
    headers = {"X-Role": "supervisor_l2"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=host), base_url="http://test") as client:
        assert (await client.get("/api/stream/status")).json()["state"] == "stopped"
        for path in ("start", "pause", "stop"):
            assert (await client.post("/api/stream/" + path, json={})).status_code == 403
        assert (await client.put("/api/detector/config", json={"threshold": 0})).status_code == 403
        result = await client.put("/api/detector/config", json={"threshold": 0}, headers=headers)
        assert result.status_code == 200 and result.json()["config"]["velocity_count"] == 5
        assert DetectorSettings(stream.repo.root).get().threshold == 0
        for values in ({"threshold": 1.1}, {"velocity_count": 1}, {"high_risk_score": .1}, {"unknown": 1}):
            assert (await client.put("/api/detector/config", json=values, headers=headers)).status_code == 422
        assert (await client.post("/api/stream/start", json={"speed_multiplier": 100}, headers=headers)).status_code == 422
        assert (await client.post("/api/stream/start", json={"start_date": "2010-01-01"}, headers=headers)).status_code == 422
        result = await client.post("/api/stream/start", json={"start_date": "2016-12-01T00:00:00Z", "mode_evidence": "human"}, headers=headers)
        assert result.status_code == 200 and result.json()["mode_evidence"] == "human"
        assert (await client.post("/api/stream/stop", headers=headers)).json()["state"] == "stopped"
    await stream.close()
    await stream.investigations.close()
