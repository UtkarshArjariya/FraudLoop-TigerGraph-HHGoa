import asyncio
import json

import pandas as pd
import pytest

from agent.local_backend import LocalBackend
from agent.store import TransactionStore


def call(backend, name, **params):
    return asyncio.run(backend.call(name, params))


@pytest.fixture
def backend():
    txns = pd.DataFrame([
        {"txn_id": "T1", "epoch": 100, "card_id": "C1-K1", "card_key": "C1-K1", "customer_id": "C1", "device_profile": "phone", "region": "home"},
        {"txn_id": "T2", "epoch": 200, "card_id": "C1-K1", "card_key": "C1-K1", "customer_id": "C1", "device_profile": "phone", "region": "home"},
        {"txn_id": "T3", "epoch": 300, "card_id": "C2-K1", "card_key": "C2-K1", "customer_id": "C2", "device_profile": "phone", "region": "away"},
        {"txn_id": "T4", "epoch": 110, "card_id": "C3-K1", "card_key": "C3-K1", "customer_id": "C3", "device_profile": "", "region": ""},
    ])
    history = pd.DataFrame([
        {"case_id": "CC-1", "card_id": "C1-K1", "customer_id": "C1", "closed_at": "1970-01-01 00:02:30", "outcome": "cleared", "txn_ids": "T1", "analyst_notes": "Customer confirmed recurring purchase", "pattern": "none"},
        {"case_id": "CC-2", "card_id": "C2-K1", "customer_id": "C2", "closed_at": "1970-01-01 00:06:40", "outcome": "confirmed_fraud", "txn_ids": "T3", "analyst_notes": "Card testing stolen phone", "pattern": "card_testing"},
    ])
    pack = pd.DataFrame([{"case_id": "HHG-1", "flagged_txn_id": "T2"}])
    return LocalBackend(TransactionStore(txns, history, pack, {}))


def test_windows_respect_cutoff_and_baseline_excludes_flagged(backend):
    assert [r["txn_id"] for r in call(backend, "card_window", card_id="C1-K1", as_of=200, hours=1)["transactions"]] == ["T1", "T2"]
    assert [r["txn_id"] for r in call(backend, "card_baseline", card_id="C1-K1", as_of=200, days=1)["transactions"]] == ["T1"]
    assert call(backend, "transaction", txn_id="T3", as_of=200)["transactions"] == []
    with pytest.raises(ValueError, match="as_of"):
        call(backend, "card_window", card_id="C1-K1")


def test_prior_and_vector_retrieval_never_reveal_future_outcomes(backend):
    backend.store.embeddings = {"CC-1": [0.0, 1.0], "CC-2": [1.0, 0.0]}
    prior = call(backend, "prior_cases", card_id="C2-K1", device_id="phone", region_id="away", as_of=250)
    assert [row["case_id"] for row in prior["cases"]] == ["CC-1"]
    found = call(backend, "similar_cases", query_vec=[1.0, 0.0], as_of=250, k=5)
    assert [row["case_id"] for row in found["cases"]] == ["CC-1"]
    assert found["retrieval_method"] == "embedding_cosine"
    assert found["cases"][0]["similarity"] == 0.0
    found = call(backend, "similar_cases", query_vec=[1.0, 0.0], as_of=500, k=1)
    assert found["cases"][0]["case_id"] == "CC-2"


def test_empty_origins_do_not_form_a_ring(backend):
    assert call(backend, "device_ring", device_id="", as_of=250, days=7)["transactions"] == []
    assert call(backend, "region_cluster", region_id="", as_of=250, days=7)["transactions"] == []
    ring = call(backend, "device_ring", device_id="phone", as_of=250, days=7)
    assert [row["txn_id"] for row in ring["transactions"]] == ["T1", "T2"]
    assert ring["cases"] == []  # The linked completed case was cleared.


def test_local_write_is_retrievable_only_by_later_cases(backend):
    result = call(backend, "write_case", case_id="HHG-1", customer_id="C1", card_id="C1-K1", opened_epoch=200,
                  verdict="legitimate", summary="Customer confirmed", answer_json=json.dumps({"case": {"affected_txn_ids": []}}))
    assert result["stored_locally"] is True
    assert result["written_to_graph"] is False
    assert result["graph_case_id"] == ""
    assert call(backend, "prior_cases", card_id="C1-K1", as_of=200)["memory"] == []
    assert call(backend, "prior_cases", card_id="C1-K1", as_of=201)["memory"][0]["case_id"] == "HHG-1"
    assert call(backend, "prior_cases", device_id="phone", as_of=201)["memory"][0]["case_id"] == "HHG-1"


def test_lexical_fallback_and_temporal_components_are_explicit(backend):
    found = call(backend, "similar_cases", query_text="card testing stolen phone", as_of=500, k=1)
    assert found["retrieval_method"] == "lexical_fallback"
    assert found["cases"][0]["case_id"] == "CC-2"
    component = call(backend, "tg_wcc", card_id="C1-K1", as_of=250)
    assert component["components"][0]["card_keys"] == ["C1-K1"]
    assert component["temporally_filtered"] is True
    component = call(backend, "tg_wcc", card_id="C1-K1", as_of=350)
    assert component["components"][0]["card_keys"] == ["C1-K1", "C2-K1"]


def test_case_library_and_document_retrieval_filter_future_history(backend):
    assert [row["case_id"] for row in call(backend, "case_library", as_of=250)["cases"]] == ["CC-1"]
    first = call(backend, "write_document", doc_id="policy", source_id="policy", text_data="Verify before blocking")
    assert first["written_to_graph"] is False
    call(backend, "write_document", doc_id="policy", source_id="policy", text_data="R1: Verify before blocking")
    assert len(backend.store.documents) == 1
    call(backend, "write_document", doc_id="future-note", source_id="CC-2", text_data="Card testing confirmed")
    docs = call(backend, "get_documents", query_text="card testing", as_of=250, k=10)
    assert [row["doc_id"] for row in docs["documents"]] == ["policy"]
    docs = call(backend, "get_documents", query_text="card testing", as_of=500, k=10)
    assert [row["doc_id"] for row in docs["documents"]] == ["future-note", "policy"]
