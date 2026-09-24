"""Bulk ingestion tests use fake SDK/graph calls and a private temp cache."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from google.genai.errors import ClientError
import pandas as pd
import pytest

from agent.store import TransactionStore
import agent.embeddings as embeddings
from scripts import embed_docs as module


VECTOR = [1.0] + [0.0] * 767


def error(code, message="quota exhausted", **extra):
    return ClientError(code, {"error": {"code": code, "status": "RESOURCE_EXHAUSTED" if code == 429 else "INVALID_ARGUMENT",
                                      "message": message, **extra}})


def row(identifier="D0", text="policy"):
    return module.EmbedRow("DocChunk", identifier, text, {}, "document")


@pytest.fixture
def environment(tmp_path, monkeypatch):
    docs = [{"doc_id": "D0", "source_id": "README", "text_data": "Policy text"}]
    history = pd.DataFrame([
        {"case_id": "CC-card", "card_id": "C1-K1", "customer_id": "C1", "txn_ids": "X", "analyst_notes": "card", "pattern": "none", "outcome": "cleared"},
        {"case_id": "CC-customer", "card_id": "C1-K2", "customer_id": "C1", "txn_ids": "X", "analyst_notes": "customer", "pattern": "none", "outcome": "cleared"},
        {"case_id": "CC-device", "card_id": "C2-K1", "customer_id": "C2", "txn_ids": "D", "analyst_notes": "device", "pattern": "card_testing", "outcome": "confirmed_fraud"},
        {"case_id": "CC-region", "card_id": "C3-K1", "customer_id": "C3", "txn_ids": "R", "analyst_notes": "region", "pattern": "none", "outcome": "cleared"},
        {"case_id": "CC-unlinked1", "card_id": "C4-K1", "customer_id": "C4", "txn_ids": "X", "analyst_notes": "sample", "pattern": "none", "outcome": "cleared"},
        {"case_id": "CC-unlinked2", "card_id": "C5-K1", "customer_id": "C5", "txn_ids": "X", "analyst_notes": "sample", "pattern": "undocumented", "outcome": "confirmed_fraud"},
    ])
    store = TransactionStore(pd.DataFrame([
        {"txn_id": "F", "device_profile": "phone", "region": "home"},
        {"txn_id": "D", "device_profile": "phone", "region": "away"},
        {"txn_id": "R", "device_profile": "other", "region": "home"},
    ]), history, pd.DataFrame([{"flagged_txn_id": "F", "card_id": "C1-K1", "customer_id": "C1"}]), {})
    store_path = tmp_path / "store.pkl"
    store.save(store_path)
    monkeypatch.setattr(module, "CACHE_DIR", tmp_path / "embeddings")
    monkeypatch.setattr(module, "STATUS_PATH", tmp_path / "status.json")
    monkeypatch.setattr(module, "discover", lambda **kwargs: {"embed": {"models": ["gemini-embedding-001", "gemini-embedding-2"]}})
    monkeypatch.setattr(module, "documents", lambda source: docs)
    monkeypatch.setattr(module, "load_dotenv", lambda: None)
    monkeypatch.setenv("USE_LOCAL", "0")
    monkeypatch.setenv("EMBED_PROVIDER", "gem001")
    sleeps, requests, uploads, document_writes = [], [], [], []

    async def sleep(seconds):
        sleeps.append(seconds)

    async def to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    def generate(texts, **kwargs):
        requests.append(texts)
        return [VECTOR[:] for _ in texts]

    class Graph:
        fail_id = None

        def __init__(self, **kwargs):
            self.tool_calls = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def call(self, query, params):
            self.tool_calls += 1
            document_writes.append(params)
            return {}

        async def reset_vectors(self):
            uploads.clear()

        async def upsert_vectors(self, vertex_type, payload, *, attribute):
            self.tool_calls += 1
            if any(item["vertex_id"] == self.fail_id for item in payload):
                raise RuntimeError("503 graph unavailable")
            uploads.append((vertex_type, payload))
            return {"success_count": len(payload)}

    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    monkeypatch.setattr(module.asyncio, "to_thread", to_thread)
    monkeypatch.setattr(module.GeminiEmbedder, "embed", lambda self, texts, **kwargs: generate(texts, **kwargs))
    monkeypatch.setattr(module, "GraphClient", Graph)
    args = SimpleNamespace(store=store_path, source=Path("unused"), batch_size=20, limit=1, local=False, dry_run=False, provider="gem001", refresh_models=False)
    return SimpleNamespace(store=store, docs=docs, args=args, sleeps=sleeps, requests=requests,
                           uploads=uploads, document_writes=document_writes, graph=Graph, monkeypatch=monkeypatch)


def test_priority_docs_then_all_four_relationships_then_stratified(environment):
    e = environment
    docs = [{"doc_id": f"D{i}", "source_id": "README", "text_data": "policy"} for i in range(12)]
    planned = module.prioritized_rows(e.store, docs, 18)
    assert [r.vertex_id for r in planned[:12]] == [f"D{i}" for i in range(12)]
    assert [r.vertex_id for r in planned[12:16]] == ["CC-card", "CC-customer", "CC-device", "CC-region"]
    assert all(r.priority == "stratified_sample" for r in planned[16:])
    assert len(module.prioritized_rows(e.store, docs, 14)) == 14
    with pytest.raises(ValueError):
        module.prioritized_rows(e.store, docs, 0)


def test_stratification_does_not_fill_sample_with_majority_outcome():
    rows = [{"case_id": f"CC-{i}", "pattern": "none", "outcome": "cleared"} for i in range(20)]
    rows.append({"case_id": "CC-rare", "pattern": "undocumented", "outcome": "confirmed_fraud"})
    assert {r["outcome"] for r in module.stratified(rows)[:2]} == {"cleared", "confirmed_fraud"}


def test_retry_delay_understands_google_details_and_text():
    assert module.retry_delay(error(429, details=[{"@type": "google.rpc.RetryInfo", "retryDelay": "7.25s"}])) == 7.25
    assert module.retry_delay(error(429, details=[{"retryDelay": {"seconds": "2", "nanos": 500000000}}])) == 2.5
    assert module.retry_delay(RuntimeError("429 RESOURCE_EXHAUSTED retryDelay: '3s'")) == 3
    assert module.retry_delay(error(429, details=[{"retryDelay": "invalid"}])) is None


def test_400_singletons_skip_empty_rejected_and_truncate(environment):
    e = environment
    e.docs.extend([{"doc_id": "empty", "source_id": "README", "text_data": "  "},
                   {"doc_id": "long", "source_id": "README", "text_data": "a" * 3000},
                   {"doc_id": "bad", "source_id": "README", "text_data": "bad"}])
    e.args.limit = 4

    def generate(texts):
        e.requests.append(texts)
        if len(texts) > 1 or texts == ["bad"]:
            raise error(400, "Invalid text input")
        return [VECTOR]

    e.monkeypatch.setattr(module.GeminiEmbedder, "embed", lambda self, texts, **kwargs: generate(texts))
    result = asyncio.run(module.run(e.args))["indexes"][0]
    assert not result["aborted"]
    assert result["truncated_ids"] == ["long"]
    assert set(result["skipped_ids"]) == {"empty", "bad"}
    assert module.completed_ids(embeddings.checkpoint_path(embeddings.BASE_INDEXES[0])) == {"D0", "long"}
    assert all(len(text) <= 2000 for batch in e.requests for text in batch)
    assert len(next(d["text_data"] for d in e.document_writes if d["doc_id"] == "long")) == 3000


def test_twenty_text_batches_sleep_four_seconds_and_cap_total(environment):
    e = environment
    e.docs[:] = [{"doc_id": f"D{i}", "source_id": "README", "text_data": f"text {i}"} for i in range(41)]
    e.args.limit = 41
    result = asyncio.run(module.run(e.args))["indexes"][0]
    assert result["completed_selected"] == 41
    assert [len(batch) for batch in e.requests] == [20, 20, 1]
    assert e.sleeps == [4, 4]
    assert all(kind == "DocChunk" for kind, _ in e.uploads)
    e.requests.clear()
    e.uploads.clear()
    result = asyncio.run(module.run(e.args))["indexes"][0]
    assert not e.requests and not e.uploads
    assert result["newly_completed"] == 0


def test_cached_vectors_resume_after_upload_failure_without_new_api_cost(environment):
    e = environment
    e.docs.append({"doc_id": "D1", "source_id": "README", "text_data": "second"})
    e.args.batch_size, e.args.limit, e.graph.fail_id = 1, 2, "D1"
    result = asyncio.run(module.run(e.args))["indexes"][0]
    assert result["aborted"]
    assert module.completed_ids(embeddings.checkpoint_path(embeddings.BASE_INDEXES[0])) == {"D0"}
    assert set(json.loads((module.CACHE_DIR / "gemini-embedding-001-768.json").read_text())) == {"D0", "D1"}
    e.requests.clear()
    e.uploads.clear()
    e.graph.fail_id = None
    result = asyncio.run(module.run(e.args))["indexes"][0]
    assert not result["aborted"] and not e.requests
    assert [p["vertex_id"] for _, batch in e.uploads for p in batch] == ["D1"]
    assert module.completed_ids(embeddings.checkpoint_path(embeddings.BASE_INDEXES[0])) == {"D0", "D1"}


def test_local_checkpoint_does_not_suppress_graph_upload(environment):
    e = environment
    e.args.local = True
    asyncio.run(module.run(e.args))
    assert not embeddings.checkpoint_path(embeddings.BASE_INDEXES[0]).exists()
    assert embeddings.covered_ids(embeddings.BASE_INDEXES[0], True) == {'D0'}
    e.args.local = False
    result = asyncio.run(module.run(e.args))["indexes"][0]
    assert result["newly_completed"] == 1
    assert embeddings.covered_ids(embeddings.BASE_INDEXES[0]) == {'D0'}


def test_env_limit_and_dry_run_make_no_requests(environment):
    e = environment
    e.args.limit, e.args.dry_run = None, True
    e.monkeypatch.setenv("EMBED_LIMIT", "3")
    result = asyncio.run(module.run(e.args))["indexes"][0]
    assert result["limit"] == 3 and result["selected"] == 3
    assert not e.requests and not e.uploads and not embeddings.checkpoint_path(embeddings.BASE_INDEXES[0]).exists()


def test_error_reporting_redacts_secret_without_truncating_diagnostics(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "super-secret-key")
    text = module.exception_message(error(400, "bad input " + "x" * 900 + " super-secret-key"))
    assert "400 INVALID_ARGUMENT" in text and "bad input" in text
    assert len(text) > 900 and "super-secret-key" not in text and "[REDACTED]" in text


def test_legacy_gem001_checkpoint_is_preserved_and_imported(environment):
    e = environment
    embeddings.LEGACY_CHECKPOINT.write_text('D0\n')
    before = embeddings.LEGACY_CHECKPOINT.read_bytes()
    result = asyncio.run(module.run(e.args))["indexes"][0]
    assert result['newly_completed'] == 0 and not e.requests
    assert embeddings.LEGACY_CHECKPOINT.read_bytes() == before
    assert embeddings.checkpoint_path(embeddings.BASE_INDEXES[0]).read_text() == 'D0\n'


def test_all_continues_after_remote_quota_and_builds_full_local_indexes(environment):
    e = environment
    e.args.provider, e.args.limit = 'all', None
    e.monkeypatch.setenv('EMBED_LIMIT', '2')
    e.monkeypatch.setattr(module.GeminiEmbedder, 'embed', lambda *args, **kwargs: (_ for _ in ()).throw(
        error(429, details=[{'violations': [{'quotaId': 'EmbedRequestsPerDay'}]}])))
    for factory in (embeddings.LocalGemmaEmbedder, embeddings.LocalBgeEmbedder):
        e.monkeypatch.setattr(factory, 'embed', lambda self, texts, **kwargs: [VECTOR for _ in texts])
    result = asyncio.run(module.run(e.args))
    rows = {r['index']: r for r in result['indexes']}
    assert rows['gem001']['aborted'] and rows['gem2']['aborted']
    assert rows['gemma']['completed_selected'] == rows['bge']['completed_selected'] == 7
    assert not rows['bge']['aborted'] and not rows['gemma']['aborted']
    assert all(p['model_id'] in (embeddings.GEMMA_MODEL, embeddings.LOCAL_MODEL) for _, batch in e.uploads for p in batch)


def test_gemma_download_failure_uses_separate_bge_index(environment):
    e = environment
    e.args.provider = 'gemma'
    e.monkeypatch.setattr(embeddings.LocalGemmaEmbedder, 'embed', lambda *args, **kwargs: (_ for _ in ()).throw(OSError('model unavailable')))
    e.monkeypatch.setattr(embeddings.LocalBgeEmbedder, 'embed', lambda self, texts, **kwargs: [VECTOR for _ in texts])
    result = asyncio.run(module.run(e.args))
    assert [r['index'] for r in result['indexes']] == ['gemma', 'bge']
    assert all(p['model_id'] == embeddings.LOCAL_MODEL for _, batch in e.uploads for p in batch)
    assert not embeddings.covered_ids(embeddings.BASE_INDEXES[2])
    assert embeddings.covered_ids(embeddings.BASE_INDEXES[3]) == {'D0'}


def test_missing_legacy_attribute_restores_cached_vectors_without_api_calls(environment):
    e = environment
    spec = embeddings.BASE_INDEXES[0]
    embeddings.LEGACY_CHECKPOINT.write_text('D0\n')
    embeddings.register_index(spec, legacy_recovery_required=True)
    e.args.cached_only = True
    module.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (module.CACHE_DIR / 'gemini-embedding-001-768.json').write_text(json.dumps({'D0': VECTOR}))
    e.monkeypatch.setattr(module.GeminiEmbedder, 'embed', lambda *args, **kwargs: pytest.fail('must use cache'))
    cache_before = (module.CACHE_DIR / 'gemini-embedding-001-768.json').read_bytes()
    assert not embeddings.covered_ids(spec)
    result = asyncio.run(module.run(e.args))['indexes'][0]
    assert result['newly_completed'] == 1 and not result['aborted']
    assert embeddings.covered_ids(spec) == {'D0'}
    assert embeddings.LEGACY_CHECKPOINT.read_text() == 'D0\n'
    assert (module.CACHE_DIR / 'gemini-embedding-001-768.json').read_bytes() == cache_before


@pytest.mark.parametrize('local', [False, True])
def test_gemma_ingestion_records_actual_fallback_device_without_changing_legacy_state(environment, gemma_model, local, capsys):
    from scripts import embed_status

    e = environment
    e.args.provider, e.args.limit, e.args.local = 'gemma', 70, local
    e.docs[:] = [{'doc_id': f'D{i}', 'source_id': 'README', 'text_data': f'text {i}'} for i in range(70)]
    original = {'model_id': embeddings.LOCAL_MODEL, 'state': 'resetting', 'dim': 768}
    embeddings.LEGACY_INDEX_PATH.write_text(json.dumps(original))
    gemma_model.torch.cuda.is_available.return_value = True
    attempts = []

    def encode(texts, **kwargs):
        attempts.append(len(texts))
        if len(attempts) <= 2:
            raise gemma_model.torch.cuda.OutOfMemoryError('allocation failed')
        return gemma_model.vectors(texts)

    gemma_model.model.encode.side_effect = encode
    result = asyncio.run(module.run(e.args))
    assert len(result['indexes']) == 1 and not result['aborted']
    status = result['indexes'][0]
    assert attempts == [64, 64, 64, 6]
    assert status['completed_selected'] == 70
    assert [c.kwargs['batch_size'] for c in gemma_model.model.encode.call_args_list] == [64, 32, 16, 16]
    runtime = {'device': 'cpu', 'batch_size': 16, 'precision': 'float32'}
    assert all(status[k] == value for k, value in runtime.items())
    saved = embeddings.read_indexes(local)['indexes']['gemma']
    assert all(saved[k] == value for k, value in runtime.items())
    legacy_path = embeddings.LEGACY_INDEX_PATH
    if local:
        assert json.loads(legacy_path.read_text()) == original
        legacy_path = legacy_path.with_name(legacy_path.stem + '.local.json')
    legacy = json.loads(legacy_path.read_text())
    if not local:
        assert all(legacy[k] == value for k, value in original.items())
    assert legacy['runtime']['gemma'] == {'model_id': embeddings.GEMMA_MODEL, **runtime}
    on_disk = json.loads((module.CACHE_DIR / ('gemma.local.status.json' if local else 'gemma.status.json')).read_text())
    assert on_disk['device'] == 'cpu'
    assert 'device=cpu batch_size=16 precision=float32' in capsys.readouterr().out
    e.monkeypatch.setattr(embed_status, 'documents', lambda _: e.docs)
    rows = {r['index']: r for r in embed_status.coverage_rows(e.store, local=local)}
    assert rows['gemma']['device'] == 'cpu' and rows['gemma']['vectors'] == 70
    assert rows['gem001']['device'] == 'unknown'
    # An already-complete cached-only run neither loads weights nor rewrites
    # runtime metadata as though the device had been used again.
    before = legacy_path.read_bytes()
    gemma_model.model.encode.reset_mock()
    e.args.cached_only = True
    asyncio.run(module.run(e.args))
    gemma_model.model.encode.assert_not_called()
    assert legacy_path.read_bytes() == before


def test_gemma_dry_run_and_cached_only_need_no_device_initialization(environment, gemma_model):
    e = environment
    e.args.provider, e.args.dry_run = 'gemma', True
    asyncio.run(module.run(e.args))
    e.args.dry_run, e.args.cached_only = False, True
    result = asyncio.run(module.run(e.args))
    assert not result['aborted'] and result['indexes'][0]['newly_completed'] == 0
    gemma_model.torch.cuda.is_available.assert_not_called()
    gemma_model.factory.assert_not_called()
    assert not embeddings.LEGACY_INDEX_PATH.exists()
