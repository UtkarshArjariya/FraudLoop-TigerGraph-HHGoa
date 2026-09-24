import asyncio
import json
from unittest.mock import MagicMock

import pytest

from agent import embeddings as emb
from agent.models import model_chain
from agent.mcp_client import GraphClient


def test_upsert_rejects_wrong_or_missing_model_before_any_graph_call():
    graph = GraphClient(use_local=False)
    for model, attr in (("", "embedding"), (emb.LOCAL_MODEL, "embedding"),
                        ("gemini-embedding-001", "emb_bge"), (emb.GEMMA_MODEL, "emb_bge")):
        with pytest.raises(emb.ModelMismatch):
            asyncio.run(graph.upsert_vectors('ClosedCase', [{'vertex_id': 'CC-1',
                'vector': [1.] + [0.] * 767, 'model_id': model}], attribute=attr))
    assert graph.tool_calls == 0


def test_manifest_cannot_remap_existing_attribute():
    spec = emb.BASE_INDEXES[3]
    emb.register_index(spec)
    with pytest.raises(emb.ModelMismatch):
        emb.register_index(emb.IndexSpec('other', 'other-model', spec.attribute, 'gemini'))


def test_exclusive_index_lock_blocks_queries_and_second_builder():
    with emb.index_lock(exclusive=True):
        with pytest.raises(emb.ModelMismatch, match='busy'):
            with emb.index_lock():
                pass
        with pytest.raises(emb.ModelMismatch, match='busy'):
            with emb.index_lock(exclusive=True):
                pass


def test_local_provider_cpu_and_query_api(monkeypatch):
    import sys
    fake = MagicMock()
    model = fake.TextEmbedding.return_value
    model.embed.return_value = [[2.] + [0.] * 767]
    model.query_embed.return_value = [[3.] + [0.] * 767]
    monkeypatch.setitem(sys.modules, 'fastembed', fake)
    provider = emb.LocalEmbedder()
    assert provider.embed(['text'])[0][0] == 1
    assert provider.embed(['query'], query=True)[0][0] == 1
    assert fake.TextEmbedding.call_args.kwargs['providers'] == ['CPUExecutionProvider']
    model.query_embed.assert_called_once_with(['query'])


def test_normalization_rejects_merged_batches_and_invalid_dimensions():
    with pytest.raises(ValueError, match='length mismatch'):
        emb.normalize([[1.] * 768], 20)
    with pytest.raises(ValueError, match='768'):
        emb.normalize([[1.] * 384], 1)


def test_migration_preserves_legacy_vectors_and_adds_all_model_attributes():
    from scripts.migrate_embeddings import schema_additions, vector_queries
    inventory = '\n'.join(f'  - VERTEX {v}(PRIMARY_ID id STRING)' for v in ('ClosedCase', 'DocChunk', 'FraudCase'))
    inventory += '\nVector Embeddings:\n' + '\n'.join(f'  - {v}:\n    - embedding(Dimension=768)' for v in ('ClosedCase', 'DocChunk', 'FraudCase'))
    script = '\n'.join(schema_additions(inventory, emb.BASE_INDEXES))
    assert 'DROP' not in script
    assert 'ADD VECTOR ATTRIBUTE embedding(' not in script
    for vertex in ('ClosedCase', 'DocChunk', 'FraudCase'):
        for attr in ('emb_gem2', 'emb_gemma', 'emb_bge'):
            assert f'ALTER VERTEX {vertex} ADD VECTOR ATTRIBUTE {attr}' in script
    queries = vector_queries(emb.BASE_INDEXES)
    assert 'ClosedCase.embedding' in queries and 'DocChunk.emb_gemma' in queries
    assert 'f.opened_epoch < as_of' in queries and 'covered_ids.contains(c.case_id)' in queries


def test_gemma_cpu_uses_exact_model_card_prompts(monkeypatch):
    import sys
    fake = MagicMock()
    fake.SentenceTransformer.return_value.encode.return_value = [[2.] + [0.] * 767]
    monkeypatch.setitem(sys.modules, 'sentence_transformers', fake)
    provider = emb.LocalGemmaEmbedder()
    assert provider.embed(['doc'])[0][0] == 1
    assert provider.embed(['question'], query=True)[0][0] == 1
    fake.SentenceTransformer.assert_called_once_with(emb.GEMMA_MODEL, device='cpu', trust_remote_code=False)
    assert [c.kwargs['prompt'] for c in fake.SentenceTransformer.return_value.encode.call_args_list] == [
        'title: none | text: ', 'task: search result | query: ']


def test_embedding_two_never_merges_documents():
    from types import SimpleNamespace
    client = MagicMock()
    client.models.embed_content.return_value = SimpleNamespace(embeddings=[SimpleNamespace(values=[2.] + [0.] * 767)])
    provider = emb.GeminiEmbedder('gemini-embedding-2', client=client)
    assert len(provider.embed(['one', 'two'])) == 2
    assert [call.kwargs['contents'] for call in client.models.embed_content.call_args_list] == [['one'], ['two']]
    assert all(call.kwargs['config'].output_dimensionality == 768 for call in client.models.embed_content.call_args_list)


def test_preview_models_get_their_own_attribute_and_model():
    specs = emb.index_specs(registry={'embed': {'models': ['gemini-embedding-2', 'gemini-embedding-2-preview']}})
    two = [s for s in specs if 'embedding-2' in s.model_id]
    assert len(two) == 2 and len({s.attribute for s in two}) == 2
