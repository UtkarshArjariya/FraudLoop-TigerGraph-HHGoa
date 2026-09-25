import asyncio
from copy import deepcopy

import pytest

from agent.rag import RagConfig
from backend.rag import ReadOnlyGraph, text_preview
from backend.tests.test_api import app, client


def test_config_role_ranges_and_atomic_persistence(client, app):
    response = client.get("/api/rag/config")
    config = response.json()["config"]
    assert config == response.json()["defaults"]
    assert client.put("/api/rag/config", json=config).status_code == 403
    headers = {"X-Role": "supervisor_l2"}
    assert client.put("/api/rag/config", json={**config, "min_similarity": 2}, headers=headers).status_code == 422
    updated = {**config, "top_k_precedents": 2, "rrf_k": 20}
    assert client.put("/api/rag/config", json=updated, headers=headers).json()["config"] == updated
    assert client.get("/api/rag/config").json()["config"] == updated
    assert (app.state.repository.data / "rag_config.json").exists()


@pytest.mark.parametrize("body", [{}, {"case_id": "HHG-014", "query_text": "fraud"},
    {"query_text": " "}, {"query_text": "x" * 2001}])
def test_preview_rejects_ambiguous_or_empty_input(client, body):
    assert client.post("/api/rag/preview", json=body).status_code == 422


def test_preview_route_preserves_graded_files_and_contract(client, app, monkeypatch):
    import backend.rag as module
    root = app.state.repository.root
    original = {path: path.read_bytes() for folder in [root / "cases", root / "data/timelines"] for path in folder.glob("*.json")}
    async def run(repo, gateway, manager, body):
        assert body.case_id == "HHG-014"
        return {"config": RagConfig().model_dump(), "precedents": [], "policy_chunks": [], "graph_paths": [],
                "evidence_brief": {}, "llm_context": "{}", "estimated_tokens": 1,
                "no_llm": True, "written_to_graph": False}
    monkeypatch.setattr(module, "preview", run)
    payload = client.post("/api/rag/preview", json={"case_id": "HHG-014"}).json()
    assert payload["no_llm"] and payload["written_to_graph"] is False
    assert all(path.read_bytes() == content for path, content in original.items())
    assert client.post("/api/rag/preview", json={"case_id": "missing"}).status_code == 404


def test_preview_graph_rejects_write_tools():
    class Graph:
        async def call(self, *_args):
            pytest.fail("A mutation escaped the read-only adapter")
    with pytest.raises(ValueError, match="forbidden"):
        asyncio.run(ReadOnlyGraph(Graph()).call("write_case", {}))
    with pytest.raises(AttributeError):
        ReadOnlyGraph(Graph()).upsert_vectors


def test_free_text_preview_fuses_retrieval_without_narration_or_graph_paths(monkeypatch):
    import backend.rag as module
    class Graph:
        use_local = True
        async def call(self, name, params=None):
            assert name in {"case_library", "get_documents"}
            return {"cases": [{"case_id": "CC-1", "analyst_notes": "card testing", "outcome": "confirmed_fraud"}],
                    "memory": [], "documents": []}
    async def retrieve(*_args, **_kwargs):
        return {"bge": [{"id": "CC-1", "similarity": .9}]}, []
    monkeypatch.setattr(module, "retrieve_indexes", retrieve)
    packet = asyncio.run(text_preview(ReadOnlyGraph(Graph()), "card testing", 100, RagConfig()))
    assert packet["stage"] == "query_retrieval_only" and packet["graph_paths"] == []
    assert packet["precedents"][0]["fused_score"] > 0
    assert {part["source"] for part in packet["precedents"][0]["provenance"]} == {"bge", "analyst narrative"}
    assert packet["evidence_brief"]["precedents"] == packet["precedents"]
