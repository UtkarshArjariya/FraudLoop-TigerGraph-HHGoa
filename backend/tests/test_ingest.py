"""Ingestion previews bind exact content; writes remain guarded and recoverable."""
from contextlib import asynccontextmanager, contextmanager
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest

from agent.embeddings import ModelMismatch
from backend.ingest import (BgeCacheUnavailable, IngestRequest, IngestService, MAX_POLICY_BYTES, cached_bge_vectors,
                            cached_bge_directory,
                            policy_preview, transaction_preview)
from backend.tests.test_api import app, write_csv


@pytest.fixture
def service(app):
    return app.state.ingest


@asynccontextmanager
async def mock_graph():
    class Graph:
        graph_name = "FraudGraph"
        def __init__(self):
            self.nodes, self.vectors = [], []
        async def tool(self, name, values):
            assert name == "add_node" and values["vertex_type"] == "DocChunk"
            self.nodes.append(deepcopy(values))
        async def upsert_vectors(self, vertex_type, values, *, attribute):
            assert vertex_type == "DocChunk" and attribute == "emb_bge"
            assert all(len(item["vector"]) == 768 and abs(sum(x*x for x in item["vector"]) - 1) < 1e-9 for item in values)
            self.vectors.extend(deepcopy(values))
    yield Graph()


def prepare_live(service):
    service.gateway.local_only = False
    async def healthy():
        return True
    service.gateway.gse_for_write = healthy
    @contextmanager
    def unlocked():
        yield
    service.index_guard = unlocked
    service.require_index = lambda: None
    service.embed = lambda texts: [[3.] + [0.] * 767 for _ in texts]
    service.record_vectors = lambda ids: None


def test_transaction_columns_ids_and_values_are_authoritative(service):
    preview = transaction_preview(service.repo, "TransactionID,TransactionAmt\n3478561,74.960\n")
    assert preview["rows"] == 1 and not preview["errors"]
    assert preview["items"][0]["amount"] == "74.96"
    changed = transaction_preview(service.repo, "txn_id,amount\n3478561,100\n")
    assert changed["errors"] and not changed["items"]
    unknown = transaction_preview(service.repo, "txn_id\nNO-SUCH-ID\n")
    assert "Unknown" in unknown["errors"][0]
    duplicate = transaction_preview(service.repo, "txn_id\n3478561\n3478561\n")
    assert duplicate["errors"]
    with pytest.raises(ValueError, match="Unsupported columns"):
        transaction_preview(service.repo, "txn_id,isFraud\n3478561,1\n")
    with pytest.raises(ValueError, match="aliases duplicate"):
        transaction_preview(service.repo, "txn_id,TransactionID\n3478561,3478561\n")
    with pytest.raises(ValueError, match="2,000-row"):
        transaction_preview(service.repo, "txn_id\n" + "3478561\n" * 2001)


def test_policy_chunks_are_stable_bounded_and_not_policy_decisions():
    content = "# Supplemental reference\n\n" + "Evidence is contextual. " * 500
    first, second = policy_preview(content), policy_preview(content.replace("\n", "\r\n"))
    assert first["chunks"] > 1
    assert [item["doc_id"] for item in first["items"]] == [item["doc_id"] for item in second["items"]]
    assert all(len(item["text_data"]) <= 1200 for item in first["items"])
    assert "Encoded bank policy" in first["disclosure"]
    with pytest.raises(ValueError, match="64 KiB"):
        policy_preview("x" * (MAX_POLICY_BYTES + 1))
    with pytest.raises(ValueError, match="nonempty"):
        policy_preview(" \n")


@pytest.mark.asyncio
async def test_preview_roles_identity_and_truthful_local_staging(app, service):
    def forbidden(*args):
        pytest.fail("A local-only staged upload must not initialize embeddings")
    service.embed = forbidden
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        body = {"action": "preview", "filename": "reference.txt", "content": "Supplemental reference text."}
        preview = (await client.post("/api/ingest/policy", json=body)).json()
        assert preview["can_commit"] and not preview["written_to_graph"]
        commit = {**body, "action": "commit", "preview_id": preview["preview_id"]}
        assert (await client.post("/api/ingest/policy", json=commit)).status_code == 403
        headers = {"X-Role": "supervisor_l2"}
        assert (await client.post("/api/ingest/policy", json={**commit, "content": "Changed"}, headers=headers)).status_code == 409
        result = await client.post("/api/ingest/policy", json=commit, headers=headers)
        assert result.status_code == 200
        receipt = result.json()
        assert receipt["status"] == "staged_locally" and not receipt["written_to_graph"]
        assert receipt["counts"] == {"total": 1, "completed": 0, "staged": 1, "failed": 0}
        assert not receipt["items"][0]["vector_written"]
        assert (service.root / "receipts" / f"{preview['preview_id']}.json").exists()


@pytest.mark.asyncio
async def test_policy_commit_uses_bge_and_checkpoint_only_after_success(service):
    prepare_live(service)
    events, graph = [], None
    @asynccontextmanager
    async def client():
        nonlocal graph
        async with mock_graph() as graph:
            yield graph
    service.gateway.client = client
    def checkpoint(ids):
        assert graph.vectors and graph.vectors[-1]["vertex_id"] == ids[0]
        events.extend(ids)
    service.record_vectors = checkpoint
    request = IngestRequest(content="# Reference\n\nHuman review remains required.", filename="reference.md")
    preview = await service.preview("policy", request)
    request = request.model_copy(update={"action": "commit", "preview_id": preview["preview_id"]})
    receipt = await service.commit("policy", request, "supervisor_l2")
    assert receipt["status"] == "committed" and receipt["written_to_graph"]
    assert receipt["counts"]["completed"] == 1 and len(events) == 1
    assert graph.nodes[0]["attributes"]["embed_model"] == "BAAI/bge-base-en-v1.5"
    again = await service.commit("policy", request, "supervisor_l2")
    assert again == receipt and len(events) == 1


@pytest.mark.asyncio
async def test_index_busy_prevents_embedding_and_graph_writes(service):
    prepare_live(service)
    @contextmanager
    def busy():
        raise ModelMismatch("operator owns lock")
        yield
    service.index_guard = busy
    def forbidden(*args):
        pytest.fail("Busy index must not initialize a model")
    service.embed = forbidden
    request = IngestRequest(content="Reference.")
    preview = await service.preview("policy", request)
    with pytest.raises(ValueError, match="Embedding index is busy"):
        await service.commit("policy", request.model_copy(update={"action": "commit", "preview_id": preview["preview_id"]}), "supervisor_l2")


@pytest.mark.asyncio
async def test_transaction_commit_revalidates_then_retries_only_failed_items(service, monkeypatch):
    prepare_live(service)
    @asynccontextmanager
    async def client():
        yield object()
    service.gateway.client = client
    attempts = []
    async def upsert(graph, row):
        attempts.append(row["txn_id"])
        if attempts == ["3478561", "3478562"]:
            raise RuntimeError("secret connection text must not appear")
    monkeypatch.setattr("backend.ingest.upsert_transaction", upsert)
    request = IngestRequest(content="txn_id,amount\n3478561,74.96\n3478562,30\n", filename="transactions.csv")
    preview = await service.preview("transactions", request)
    request = request.model_copy(update={"action": "commit", "preview_id": preview["preview_id"]})
    first = await service.commit("transactions", request, "supervisor_l2")
    assert first["status"] == "partial" and first["counts"]["completed"] == 1
    assert "secret connection" not in first["warning"]
    async def unavailable():
        return False
    service.gateway.gse_for_write = unavailable
    staged = await service.commit("transactions", request, "supervisor_l2")
    assert staged["status"] == "partial" and not staged["written_to_graph"]
    assert staged["counts"] == {"total": 2, "completed": 1, "staged": 1, "failed": 0}
    assert staged["items"][0]["id"] == "3478561" and staged["items"][0]["written_to_graph"]
    prepare_live(service)
    @asynccontextmanager
    async def failed_connection():
        raise RuntimeError("synthetic MCP initialization failure")
        yield
    service.gateway.client = failed_connection
    failed = await service.commit("transactions", request, "supervisor_l2")
    assert failed["counts"]["completed"] == 1 and failed["status"] == "partial"
    service.gateway.client = client
    final = await service.commit("transactions", request, "supervisor_l2")
    assert final["status"] == "committed"
    assert attempts == ["3478561", "3478562", "3478562"]


@pytest.mark.asyncio
async def test_authoritative_change_after_preview_invalidates_commit(service):
    request = IngestRequest(content="txn_id,amount\n3478561,74.96\n")
    preview = await service.preview("transactions", request)
    write_csv(service.repo.data / "graph/transactions.csv", [{"txn_id": "3478561", "amount": "100"}])
    with pytest.raises(ValueError, match="Authoritative data changed"):
        await service.commit("transactions", request.model_copy(update={"action": "commit", "preview_id": preview["preview_id"]}), "supervisor_l2")


def test_cached_bge_never_downloads_models(monkeypatch):
    import fastembed
    import agent.embeddings as embeddings
    calls = []
    class Cached:
        def __init__(self, **kwargs):
            calls.append(kwargs)
        def embed(self, texts):
            return [[2.] + [0.] * 767 for _ in texts]
    provider = SimpleNamespace(_model=None)
    provider.embed = lambda texts: provider._model.embed(texts)
    monkeypatch.setattr(embeddings, "provider_for", lambda model: provider)
    monkeypatch.setattr("backend.ingest.cached_bge_directory", lambda: "/synthetic-cached-bge")
    monkeypatch.setattr(fastembed, "TextEmbedding", Cached)
    vectors = cached_bge_vectors(["Synthetic reference"])
    assert calls[0]["local_files_only"] is True
    assert calls[0]["providers"] == ["CPUExecutionProvider"]
    assert calls[0]["specific_model_path"] == "/synthetic-cached-bge"
    assert vectors[0] == [1.] + [0.] * 767


def test_cache_discovery_finds_existing_huggingface_snapshot_without_download(tmp_path, monkeypatch):
    monkeypatch.setenv("FASTEMBED_CACHE_PATH", str(tmp_path / "empty"))
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "hub"))
    monkeypatch.delenv("FRAUDLOOP_BGE_MODEL_PATH", raising=False)
    assert cached_bge_directory() is None
    model = tmp_path / "hub/models--Qdrant--bge-base-en-v1.5-onnx-Q/snapshots/revision"
    model.mkdir(parents=True)
    for name in ("model_optimized.onnx", "tokenizer.json", "config.json"):
        (model / name).write_text("test")
    assert cached_bge_directory() == model


@pytest.mark.asyncio
async def test_missing_cached_bge_stages_policy_with_actionable_warning_without_writes(service, monkeypatch):
    import agent.embeddings as embeddings
    prepare_live(service)
    service.embed = cached_bge_vectors
    monkeypatch.setattr(embeddings, "provider_for", lambda model: SimpleNamespace(_model=None))
    monkeypatch.setattr("backend.ingest.cached_bge_directory", lambda: None)
    @asynccontextmanager
    async def forbidden():
        pytest.fail("Missing model weights must be detected before a graph session or write")
        yield
    service.gateway.client = forbidden
    request = IngestRequest(content="New reference text.")
    preview = await service.preview("policy", request)
    receipt = await service.commit("policy", request.model_copy(update={"action": "commit", "preview_id": preview["preview_id"]}), "supervisor_l2")
    assert receipt["status"] == "staged_locally" and not receipt["written_to_graph"]
    assert receipt["counts"]["staged"] == 1
    assert "Cached BGE weights are missing" in receipt["warning"]
    assert "FASTEMBED_CACHE_PATH" in receipt["warning"]
    assert "no model was downloaded" in receipt["warning"]
