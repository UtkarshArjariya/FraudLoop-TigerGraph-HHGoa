import asyncio
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from agent.assess import rank_precedents
from agent.rag import RagConfig, build_evidence_brief
from agent.retrieval import (reciprocal_rank_fusion, eligible_library, graph_paths,
                             sorted_vector_hits, retrieve_indexes)


@pytest.mark.parametrize("change", [{"rrf_k": 0}, {"top_k_precedents": 21}, {"graph_hop_depth": 5},
    {"time_window_days": 0}, {"min_similarity": float("nan")}, {"graph_similarity_weight": -1},
    {"enabled_indexes": {"unknown": True}}, {"rrf_weights": {"bge": 6}}, {"rrf_k": True}])
def test_config_rejects_invalid_ranges_and_unknown_indexes(change):
    with pytest.raises(ValidationError):
        RagConfig.model_validate(change)


def test_rrf_weights_and_k_change_real_order_and_contributions():
    ranks = {"bge": ["A", "B"], "gem001": ["B", "A"], "graph": ["B"]}
    result = reciprocal_rank_fusion(ranks, k=1, weights={"bge": 5, "gem001": 0, "graph": 0})
    assert [row["id"] for row in result] == ["A", "B"]
    assert result[0]["fused_score"] == 2.5
    assert result[0]["provenance"] == [{"source": "bge", "rank": 1, "weight": 5,
        "contribution": 2.5, "paths": []}]
    assert reciprocal_rank_fusion(ranks, k=60)[0]["id"] == "B"


def test_cosine_threshold_does_not_treat_graph_scores_as_vectors():
    hits = {"hits": [{"case_id": "high"}, {"case_id": "low"},
        {"case_id": "graph", "similarity": 1, "retrieval_method": "graph_fallback"}],
        "distances": {"high": .1, "low": .6}}
    result = sorted_vector_hits(hits, id_key="case_id", allowed={"high", "low", "graph"},
                                min_similarity=.8, with_scores=True)
    assert result == [{"id": "high", "similarity": .9}]


def test_time_window_enforces_closed_history_and_strict_earlier_memory():
    library = {"cases": [{"case_id": "recent", "closed_at": "2026-01-09"},
                         {"case_id": "old", "closed_at": "2025-01-01"},
                         {"case_id": "future", "closed_at": "2026-01-11"}],
        "memory": [{"case_id": "before", "opened_epoch": 1767916800},
                   {"case_id": "same_time", "opened_epoch": 1768003200}]}
    found = eligible_library(library, 1768003200, {"time_window_days": 2})
    assert [row["case_id"] for row in found["cases"]] == ["recent"]
    assert [row["case_id"] for row in found["memory"]] == ["before"]


def test_hop_depth_removes_graph_contributions_but_keeps_pattern_retrieval():
    prior = {"direct_cases": [{"case_id": "A"}], "device_cases": [{"case_id": "B"}],
             "region_cases": [{"case_id": "C"}]}
    paths = graph_paths(prior, config={"graph_hop_depth": 1})
    assert paths["A"] == ["shared_card"] and not paths["B"] and not paths["C"]
    features = {"split_burst": False, "coordinated_device": False, "ordinary_amount": True,
                "familiar_product": True, "familiar_region": True, "amount_anomaly": False}
    cases = [{"case_id": key, "card_id": "Card1", "pattern": "none", "outcome": "cleared"}
             for key in ["A", "B", "C"]]
    rows = rank_precedents(cases, {"card_id": "Card1", "channel": "in_person"}, features, set(),
        vector_rankings={}, paths=graph_paths(prior), rag_config={"graph_hop_depth": 0, "top_k_precedents": 1})
    assert len(rows) == 1
    assert {part["source"] for part in rows[0]["provenance"]} == {"pattern", "analyst narrative"}
    assert all("same_pattern" not in part["paths"] for part in rows[0]["provenance"])


def test_disabled_indexes_never_embed_and_policy_top_k_is_operational(monkeypatch):
    import agent.retrieval as module
    specs = [SimpleNamespace(key=key, model_id=key, attribute="emb_" + key) for key in ("bge", "gemma")]
    monkeypatch.setattr(module, "index_specs", lambda *_: specs)
    monkeypatch.setattr(module, "covered_ids", lambda *_: {"CC-1", "doc-1", "doc-2"})
    providers = []
    monkeypatch.setattr(module, "provider_for", lambda model: SimpleNamespace(model_id=model))
    async def embed(provider, *_args, **_kwargs):
        providers.append(provider.model_id)
        return [[1.0] * 768]
    monkeypatch.setattr(module, "embed_vectors", embed)
    class Graph:
        use_local = False
        async def call(self, name, params):
            key, identifier = ("case_id", "CC-1") if name == "similar_cases" else ("doc_id", "doc-2")
            return {"hits": [{key: identifier, "similarity": .9}]}
    config = RagConfig.model_validate({"enabled_indexes": {"gemma": False}, "top_k_policy_chunks": 1})
    cases, docs = asyncio.run(retrieve_indexes(Graph(), "fraud", 100, {"cases": [{"case_id": "CC-1"}]},
        [{"doc_id": "doc-1", "text_data": "fraud"}, {"doc_id": "doc-2", "text_data": "fraud"}],
        {"warnings": []}, config=config))
    assert providers == ["bge"] and set(cases) == {"bge"}
    assert len(docs) == 1 and docs[0]["doc_id"] == "doc-2"


def test_compact_brief_strips_vectors_and_unverified_notes():
    result = SimpleNamespace(verdict="uncertain", probability=.4, pattern="none", exposure=10,
        evidence=[], episode=[], precedents=[{"case_id": "CC-1", "analyst_notes": "x" * 1000, "emb_bge": [1] * 768}])
    brief = build_evidence_brief({"case_id": "CASE-1", "card_id": "Card1", "customer_id": "Customer1"}, result,
        [{"doc_id": "policy", "text_data": "y" * 2000, "embedding": [1] * 768}],
        [{"source": "human", "assumed_response": "Typed human denial", "note": "unverified content"}], [], [], RagConfig())
    assert "emb_bge" not in brief["precedents"][0] and "embedding" not in brief["documents"][0]
    assert brief["documents"][0]["text_truncated"] and brief["precedents"][0]["notes_truncated"]
    assert "note" not in brief["requests"][0]
