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


def test_gemma_cpu_uses_exact_model_card_prompts(gemma_model):
    provider = emb.LocalGemmaEmbedder()
    assert provider.embed(['doc'])[0][0] == 1
    assert provider.embed(['question'], query=True)[0][0] == 1
    gemma_model.factory.assert_called_once_with(emb.GEMMA_MODEL, device='cpu', trust_remote_code=False)
    assert [c.kwargs['prompt'] for c in gemma_model.model.encode.call_args_list] == [
        'title: none | text: ', 'task: search result | query: ']


@pytest.mark.parametrize('requested,available,device,batch_size', [
    (None, False, 'cpu', 16), (None, True, 'cuda', 64),
    ('auto', False, 'cpu', 16), ('auto', True, 'cuda', 64),
    ('cpu', True, 'cpu', 16), ('cuda', True, 'cuda', 64),
    ('cuda', False, 'cpu', 16),
])
def test_gemma_selects_device_and_precision(monkeypatch, gemma_model, requested, available, device, batch_size):
    if requested is None:
        monkeypatch.delenv('EMBED_DEVICE')
    else:
        monkeypatch.setenv('EMBED_DEVICE', requested)
    gemma_model.torch.cuda.is_available.return_value = available
    provider = emb.LocalGemmaEmbedder()
    assert provider.embed(['document'])[0][0] == 1
    gemma_model.factory.assert_called_once_with(emb.GEMMA_MODEL, device=device, trust_remote_code=False)
    assert gemma_model.model.encode.call_args.kwargs['batch_size'] == batch_size
    assert provider.runtime == {'device': device, 'batch_size': batch_size,
                                'precision': 'float16' if device == 'cuda' else 'float32'}
    assert gemma_model.model.half.call_count == int(device == 'cuda')
    assert gemma_model.model.float.call_count == int(device == 'cpu')
    if requested == 'cpu':
        gemma_model.torch.cuda.is_available.assert_not_called()


def test_gemma_invalid_device_fails_before_model_loading(monkeypatch, gemma_model):
    monkeypatch.setenv('EMBED_DEVICE', 'cdua')
    with pytest.raises(ValueError, match='EMBED_DEVICE'):
        emb.LocalGemmaEmbedder().embed(['document'])
    gemma_model.factory.assert_not_called()


def test_empty_gemma_input_needs_no_optional_dependencies(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, 'torch', None)
    monkeypatch.setitem(sys.modules, 'sentence_transformers', None)
    provider = emb.LocalGemmaEmbedder()
    assert provider.embed([]) == [] and provider.runtime == {}


def test_gemma_cuda_oom_retries_half_batch_and_retains_it(gemma_model, caplog):
    gemma_model.torch.cuda.is_available.return_value = True
    texts = ['one', 'two']
    gemma_model.model.encode.side_effect = [
        gemma_model.torch.cuda.OutOfMemoryError('allocation failed'),
        gemma_model.vectors(texts), gemma_model.vectors(['query'])]
    provider = emb.LocalGemmaEmbedder()
    assert len(provider.embed(texts)) == 2
    assert len(provider.embed(['query'], query=True)) == 1
    calls = gemma_model.model.encode.call_args_list
    assert [c.kwargs['batch_size'] for c in calls] == [64, 32, 32]
    assert [c.args[0] for c in calls] == [texts, texts, ['query']]
    assert provider.runtime['device'] == 'cuda' and provider.runtime['batch_size'] == 32
    gemma_model.model.to.assert_not_called()
    gemma_model.torch.cuda.empty_cache.assert_called_once()
    assert 'retrying with batch_size=32' in caplog.text


def test_gemma_repeated_cuda_oom_moves_to_cpu_before_float_and_stays_there(gemma_model, caplog):
    gemma_model.torch.cuda.is_available.return_value = True
    gemma_model.model.encode.side_effect = [
        gemma_model.torch.cuda.OutOfMemoryError('allocation failed'),
        RuntimeError('CUDA out of memory. Tried to allocate'),
        gemma_model.vectors(['doc']), gemma_model.vectors(['query'])]
    provider = emb.LocalGemmaEmbedder()
    provider.embed(['doc'])
    provider.embed(['query'], query=True)
    assert [c.kwargs['batch_size'] for c in gemma_model.model.encode.call_args_list] == [64, 32, 16, 16]
    names = [c[0] for c in gemma_model.model.method_calls]
    assert names.index('to') < names.index('float')
    gemma_model.model.to.assert_called_once_with('cpu')
    assert provider.runtime == {'device': 'cpu', 'batch_size': 16, 'precision': 'float32'}
    assert gemma_model.torch.cuda.empty_cache.call_count == 2
    assert 'using CPU' in caplog.text


@pytest.mark.parametrize('stage', ['load', 'half'])
def test_gemma_cuda_initialization_oom_can_fall_back_to_cpu(gemma_model, stage):
    gemma_model.torch.cuda.is_available.return_value = True
    errors = [gemma_model.torch.cuda.OutOfMemoryError('allocation failed') for _ in range(2)]
    if stage == 'load':
        gemma_model.factory.side_effect = errors + [gemma_model.model]
    else:
        gemma_model.model.half.side_effect = errors
    provider = emb.LocalGemmaEmbedder()
    assert provider.embed(['doc'])[0][0] == 1
    assert [c.kwargs['device'] for c in gemma_model.factory.call_args_list] == ['cuda', 'cuda', 'cpu']
    assert provider.runtime['device'] == 'cpu'


@pytest.mark.parametrize('available,error', [
    (True, RuntimeError('CUDA invalid device function')),
    (True, ValueError('invalid input')),
    (False, RuntimeError('CPU out of memory')),
])
def test_gemma_does_not_retry_unrelated_or_cpu_errors(gemma_model, available, error):
    gemma_model.torch.cuda.is_available.return_value = available
    gemma_model.model.encode.side_effect = error
    provider = emb.LocalGemmaEmbedder()
    with pytest.raises(type(error), match=str(error)):
        provider.embed(['doc'])
    assert gemma_model.model.encode.call_count == 1 and provider.runtime == {}
    gemma_model.torch.cuda.empty_cache.assert_not_called()


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


def test_migration_requires_server_confirmation_even_when_installer_returns_zero(tmp_path, monkeypatch):
    from scripts import migrate_embeddings as migration
    calls = []
    def run(command, log):
        calls.append(command[-1])
        return 'Queries:\n  - similar_cases_gem001() (not installed)\n'
    monkeypatch.setattr(migration, 'run', run)
    with pytest.raises(RuntimeError, match='installation incomplete'):
        migration.install_queries(['docker'], ['similar_cases_gem001'], tmp_path / 'log')
    assert any('INSTALL QUERY' in command for command in calls)


def test_migration_resume_skips_confirmed_installed_queries(tmp_path, monkeypatch):
    from scripts import migrate_embeddings as migration
    inventory = 'Queries:\n  - similar_cases_gem001() (installed v2)\n  - old_query() (deprecated)\n'
    commands = []
    def run(command, log):
        commands.append(command[-1])
        return inventory
    monkeypatch.setattr(migration, 'run', run)
    migration.install_queries(['docker'], ['similar_cases_gem001'], tmp_path / 'log', resume=True)
    assert not any('INSTALL QUERY' in command for command in commands)
    assert migration.installed_queries(inventory) == {'similar_cases_gem001'}


def test_reserved_attribute_cannot_be_claimed_even_in_an_empty_manifest():
    with pytest.raises(emb.ModelMismatch):
        emb.register_index(emb.IndexSpec('other', emb.GEMMA_MODEL, 'emb_bge', 'gemma'))
    with pytest.raises(emb.ModelMismatch):
        emb.register_index(emb.IndexSpec('bge', 'different-model', 'other_attribute', 'gemini'))
