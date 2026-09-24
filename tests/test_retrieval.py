from agent.assess import rank_precedents
from agent.mcp_client import vector_similarities


def test_gsql_distances_not_unordered_hit_positions_determine_similarity():
    result = {"hits": [{"case_id": "CC-low"}, {"case_id": "CC-high"}],
              "distances": {"CC-high": .05, "CC-low": .9}}
    scores = vector_similarities(result)
    assert scores["CC-high"] > scores["CC-low"]


def test_local_graph_fallback_scores_are_not_mislabelled_as_vectors():
    result = {"hits": [{"case_id": "CC-vector", "similarity": .7, "retrieval_method": "embedding_cosine"},
                       {"case_id": "CC-graph", "similarity": .8, "retrieval_method": "graph_fallback"}]}
    assert vector_similarities(result) == {"CC-vector": .7}


def test_hnsw_roundoff_is_clamped_to_valid_cosine_range():
    result = {"hits": [{"case_id": "CC-1"}], "distances": {"CC-1": -5.960464e-7}}
    assert vector_similarities(result) == {"CC-1": 1.0}


def test_live_precedent_ranking_keeps_unembedded_case_and_names_method():
    features = {"split_burst": False, "coordinated_device": False, "card_testing": False,
                "ordinary_amount": True, "familiar_product": True, "familiar_region": True,
                "amount_anomaly": False, "new_device": False}
    flag = {"card_id": "C1-K1", "channel": "in_person", "DeviceInfo": ""}
    cases = [{"case_id": "CC-vector", "card_id": "C2-K1", "outcome": "confirmed_fraud", "pattern": "card_testing", "analyst_notes": "Testing"},
             {"case_id": "CC-graph", "card_id": "C1-K1", "outcome": "cleared", "pattern": "none", "analyst_notes": "Confirmed travel"}]
    ranked = rank_precedents(cases, flag, features, {"CC-graph"}, {"CC-vector": .2})
    assert ranked[0]["case_id"] == "CC-graph"
    assert ranked[0]["retrieval_method"] == "graph_fallback"
    assert "same pattern hypothesis" in ranked[0]["retrieval_basis"]
    assert ranked[1]["retrieval_method"] == "vector+graph"


def test_rrf_rewards_agreement_deduplicates_and_breaks_ties_stably():
    from agent.retrieval import reciprocal_rank_fusion, match_label
    result = reciprocal_rank_fusion({'gemma': ['A', 'A', 'B'], 'gem001': ['B', 'C'],
        'graph': [{'id': 'B', 'paths': ['shared_device']}]})
    assert [r['id'] for r in result] == ['B', 'A', 'C']
    assert abs(result[0]['fused_score'] - (1/62 + 2/61)) < 1e-10
    assert match_label(result[0]['provenance']) == 'matched by gemma + gem001 + shared device'
    tied = reciprocal_rank_fusion({'one': ['B'], 'two': ['A']})
    assert [r['id'] for r in tied] == ['A', 'B']


def test_unembedded_graph_match_remains_in_rrf_with_provenance():
    features = {'split_burst': False, 'coordinated_device': False, 'ordinary_amount': True,
                'familiar_product': True, 'familiar_region': True, 'amount_anomaly': False}
    cases = [{'case_id': 'CC-graph', 'card_id': 'C1-K1', 'pattern': 'none', 'outcome': 'cleared'},
             {'case_id': 'CC-vector', 'card_id': 'C2-K1', 'pattern': 'card_testing', 'outcome': 'confirmed_fraud'}]
    ranked = rank_precedents(cases, {'card_id': 'C1-K1', 'channel': 'in_person'}, features, {'CC-graph'},
                            vector_rankings={'gemma': ['CC-vector']}, paths={'CC-graph': ['shared_device']})
    graph = next(r for r in ranked if r['case_id'] == 'CC-graph')
    assert graph['fused_score'] > 0 and 'shared device' in graph['retrieval_basis']
    assert graph['retrieval_method'] == 'rrf'


def test_partial_indexes_query_only_covered_temporally_eligible_ids(monkeypatch):
    import asyncio
    import agent.embeddings as emb
    import agent.retrieval as retrieval
    from types import SimpleNamespace
    for spec in (emb.BASE_INDEXES[0], emb.BASE_INDEXES[2], emb.BASE_INDEXES[3]):
        path = emb.checkpoint_path(spec)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('CC-past\nCC-future\ndoc-one\n')
    providers = []
    async def embed(provider, texts, **kwargs):
        providers.append(provider.model_id)
        return [[1.] + [0.] * 767]
    monkeypatch.setattr(retrieval, 'provider_for', lambda model: SimpleNamespace(model_id=model))
    monkeypatch.setattr(retrieval, 'embed_vectors', embed)
    calls = []
    class Graph:
        use_local = False
        async def call(self, name, params):
            calls.append((name, params))
            identifier = 'CC-past' if name == 'similar_cases' else 'doc-one'
            key = 'case_id' if name == 'similar_cases' else 'doc_id'
            return {'hits': [{key: identifier}], 'distances': {identifier: .1}}
    trace = {'warnings': []}
    cases, docs = asyncio.run(retrieval.retrieve_indexes(Graph(), 'fraud policy', 100,
        {'cases': [{'case_id': 'CC-past'}], 'memory': []}, [{'doc_id': 'doc-one', 'text_data': 'policy'}], trace))
    assert set(cases) == {'gem001', 'gemma', 'bge'}
    assert all('CC-future' not in params['covered_ids'] for _, params in calls)
    assert set(providers) == {'gemini-embedding-001', emb.GEMMA_MODEL, emb.LOCAL_MODEL}
    assert len(docs[0]['provenance']) == 4
    assert docs[0]['fused_score'] > 0 and trace['document_retrieval']
