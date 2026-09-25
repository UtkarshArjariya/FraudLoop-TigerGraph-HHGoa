import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pandas as pd
import pytest

from backend.data import epoch
from backend.patterns import (PatternData, component_candidates, load_pattern_data,
                              discover_patterns, COMPONENT_SOURCE)
from backend.tests.test_api import app, client, write_csv


def row(txn, card, customer, device="phone|OS|browser|screen", at="2016-12-01 00:00:00"):
    return {"txn_id": txn, "epoch": epoch(at), "card_id": card,
            "customer_id": customer, "device_profile": device}


def data(rows, history=None):
    frame = pd.DataFrame(rows)
    maximum = int(frame.epoch.max())
    return PatternData(frame, history or [], maximum, maximum - 30 * 86400, len(rows))


def trio(device="phone|OS|browser|screen"):
    return [row(f"347856{i}", f"C{i}-K1", f"C{i}", device) for i in range(1, 4)]


def test_components_require_three_vertices_two_cards_and_mixed_customers():
    rows = trio() + [row("3478591", "C4-K1", "C4", "second|OS|browser|screen"),
                    row("3478592", "C5-K1", "C4", "second|OS|browser|screen")]
    result = component_candidates(data(rows))
    assert result["candidate_count"] == 1
    component = result["components"][0]
    assert component["node_count"] == component["size"] == 4
    assert component["card_count"] == 3 and component["customer_count"] == 3
    assert component["component_source"] == COMPONENT_SOURCE
    assert component["trigger_payload"]["type"] == "analyst_request"
    assert component["anchor_txn_id"] in component["transaction_ids"]
    assert "weak evidence" in component["trigger_payload"]["note"]
    same_customer = [{**item, "customer_id": "C1"} for item in trio()]
    assert component_candidates(data(same_customer))["candidate_count"] == 0
    two_cards = component_candidates(data(trio()[:2]))["components"][0]
    assert two_cards["node_count"] == 3 and two_cards["card_count"] == 2


def test_known_patterns_excluded_only_after_confirmed_closure_and_before_anchor():
    rows = trio()
    def history(at, outcome="confirmed_fraud", pattern="card_testing"):
        return [{"case_id": "CC-1", "card_id": "C1-K1", "closed_at": at,
                 "outcome": outcome, "pattern": pattern}]
    assert component_candidates(data(rows, history("2016-11-30")))["candidate_count"] == 0
    assert component_candidates(data(rows, history("2016-12-02")))["candidate_count"] == 1
    assert component_candidates(data(rows, history("2016-11-30", pattern="undocumented")))["candidate_count"] == 1
    assert component_candidates(data(rows, history("2016-11-30", outcome="cleared")))["candidate_count"] == 1
    # A later unrelated transaction defines the dataset end, but cannot make a
    # later historical label evidence for an earlier component's review.
    rows.append(row("3478999", "C99-K1", "C99", "other|OS|browser|screen", at="2016-12-05"))
    assert component_candidates(data(rows, history("2016-12-02")))["candidate_count"] == 1


def test_documented_transaction_link_and_connected_card_also_exclude():
    rows = trio()
    history = [{"case_id": "CC-1", "card_id": "C99-K1", "closed_at": "2016-11-30",
                "outcome": "confirmed_fraud", "pattern": "account_takeover", "txn_ids": "3478561"}]
    assert component_candidates(data(rows, history))["candidate_count"] == 0
    history[0].update(txn_ids="", connected_card_ids="C2-K1")
    assert component_candidates(data(rows, history))["candidate_count"] == 0


def test_chunked_source_filters_window_unknown_cards_and_incomplete_profiles(app, monkeypatch):
    repo = app.state.repository
    rows = trio() + [row("3478500", "C8-K1", "C8", at="2016-01-01"),
                    row("3478501", "unresolved:test", "C9"),
                    row("3478502", "C10-K1", "C10", "|||"),
                    row("3478503", "C11-K1", "C11", "phone||browser|screen")]
    for item in rows:
        item["forbidden_label"] = "must not load"
    write_csv(repo.data / "graph/transactions.csv", rows)
    calls, original = [], pd.read_csv
    def read(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)
    monkeypatch.setattr(pd, "read_csv", read)
    loaded = load_pattern_data(repo, 30, chunk_size=1)
    assert set(loaded.rows.card_id) == {"C1-K1", "C2-K1", "C3-K1"}
    assert all(value["chunksize"] == 1 and "usecols" in value for value in calls)
    assert "forbidden_label" not in loaded.rows
    assert loaded.source_rows == 7


def test_generic_profile_is_linear_and_output_samples_are_bounded():
    rows = [row(str(3400000 + i), f"C{i}-K1", f"C{i}") for i in range(250)]
    result = component_candidates(data(rows))
    assert result["prepared_edge_count"] == 250
    component = result["components"][0]
    assert component["card_count"] == 250 and component["node_count"] == 251 and len(component["card_ids"]) == 60
    assert len(component["graph_paths"]) == 20 and component["samples_truncated"]


@pytest.mark.asyncio
async def test_native_wcc_called_read_only_and_kept_separate_from_windowed_details(app, monkeypatch):
    import backend.patterns as module
    monkeypatch.setattr(module, "load_pattern_data", lambda *_: data(trio()))
    calls = []
    class Graph:
        async def call(self, name, params):
            calls.append((name, params))
            assert name == "tg_wcc"
            return {"vertex_count": 500, "component_count": 12, "needs_more_iterations": False}
    @asynccontextmanager
    async def client():
        yield Graph()
    gateway = SimpleNamespace(local_only=False, lock=asyncio.Lock(), client=client)
    result = await discover_patterns(app.state.repository, gateway, 30)
    assert calls == [("tg_wcc", {"max_iter": 100})]
    assert result["native_wcc"]["component_count"] == 12
    assert result["prepared_component_count"] == 1
    assert result["native_wcc_scope"] == "whole_graph_unwindowed_aggregate_only"
    assert result["written_to_graph"] is False and result["no_llm"]


@pytest.mark.asyncio
async def test_unavailable_native_wcc_returns_disclosed_local_details(app, monkeypatch):
    import backend.patterns as module
    monkeypatch.setattr(module, "load_pattern_data", lambda *_: data(trio()))
    @asynccontextmanager
    async def unavailable():
        raise RuntimeError("private connection details")
        yield
    result = await discover_patterns(app.state.repository,
        SimpleNamespace(local_only=False, lock=asyncio.Lock(), client=unavailable), 30)
    assert result["native_wcc"] is None and len(result["components"]) == 1
    assert "RuntimeError" in result["warnings"][0] and "private" not in result["warnings"][0]


def test_api_ranges_and_discovery_preserve_cases_and_timelines(client, app):
    repo = app.state.repository
    original = {path: path.read_bytes() for folder in (repo.cases, repo.data / "timelines") for path in folder.glob("*.json")}
    for body in ({"window_days": 0}, {"window_days": 184}, {"window_days": True}, {"unknown": 1}):
        assert client.post("/api/patterns/discover", json=body).status_code == 422
    response = client.post("/api/patterns/discover", json={"window_days": 30})
    assert response.status_code == 200
    value = response.json()
    assert len(value["components"]) == 1 and value["native_wcc"] is None
    assert value["component_source"] == COMPONENT_SOURCE
    assert all(path.read_bytes() == content for path, content in original.items())
    assert not repo.triggers_path.exists()


def test_open_review_preserves_actual_component_anchor_over_later_card_transaction(client, app):
    repo = app.state.repository
    rows = trio() + [row("3478999", "C3-K1", "C3", device="", at="2016-12-02")]
    write_csv(repo.data / "graph/transactions.csv", rows)
    discovered = client.post("/api/patterns/discover", json={"window_days": 30}).json()
    component = discovered["components"][0]
    assert component["anchor_txn_id"] == "3478563"
    assert repo.find_transaction(card_id="C3-K1")["txn_id"] == "3478999"
    response = client.post("/api/patterns/open", json=component["open_payload"])
    assert response.status_code == 201
    value = response.json()
    trigger = value["trigger"]
    assert trigger["flagged_txn_id"] == "3478563"
    assert trigger["opened_at"] == "2016-12-01 00:00:00"
    assert trigger["trigger_type"] == "analyst_request" and trigger["runtime_case"]
    assert trigger["pattern_discovery"]["hypothesis_only"]
    assert repo.triggers()[value["case_id"]] == trigger
    assert not (repo.cases / f"{value['case_id']}.json").exists()
    stale = {**component["open_payload"], "component_id": "PC-000000000000"}
    assert client.post("/api/patterns/open", json=stale).status_code == 409
