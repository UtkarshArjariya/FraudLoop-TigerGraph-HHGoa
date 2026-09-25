"""Read-only GraphRAG previews and supervisor-controlled retrieval settings."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import re
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent.rag import (RagConfig, load_config, compact_precedent, compact_document,
                       context_packet)
from agent.retrieval import eligible_library, reciprocal_rank_fusion, retrieve_indexes, match_label
from backend.data import atomic_json


class PreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str | None = Field(None, pattern=r"^[A-Za-z0-9_-]{1,80}$")
    query_text: str | None = Field(None, min_length=1, max_length=2000)

    @model_validator(mode="after")
    def one_query(self):
        if (self.case_id is None) == (self.query_text is None):
            raise ValueError("Supply exactly one of case_id or query_text.")
        if self.query_text is not None and not self.query_text.strip():
            raise ValueError("Query text must contain text.")
        return self


class ReadOnlyGraph:
    """Defense in depth: previews cannot invoke write tools, even by accident."""
    def __init__(self, graph):
        self.graph = graph

    def __getattr__(self, name):
        if name in {"tool", "upsert_vectors"}:
            raise AttributeError("Preview graph has no mutation or raw-tool interface")
        return getattr(self.graph, name)

    async def call(self, name, params=None):
        allowed = {"card_window", "card_baseline", "device_ring", "region_cluster", "prior_cases",
                   "case_library", "get_documents", "similar_cases", "search_documents"}
        if name not in allowed:
            raise ValueError("Graph writes are forbidden during a retrieval preview.")
        return await self.graph.call(name, params)


async def text_preview(graph, query_text, as_of, config):
    trace = {"warnings": [], "backend": "local" if graph.use_local else "tigergraph-mcp"}
    library = eligible_library(await graph.call("case_library", {"as_of": as_of}), as_of, config)
    documents = (await graph.call("get_documents", {})).get("documents", [])
    rankings, documents = await retrieve_indexes(graph, query_text, as_of, library, documents, trace, config=config)
    rows = library["cases"] + library["memory"]
    tokens = set(re.findall(r"\w+", query_text.lower()))
    scored = [(len(tokens & set(re.findall(r"\w+", " ".join(str(row.get(key, "")) for key in
               ("analyst_notes", "summary", "pattern", "case_id", "card_id")).lower()))), row) for row in rows]
    rankings["analyst narrative"] = [row["case_id"] for score, row in sorted(scored,
        key=lambda pair: (-pair[0], pair[1]["case_id"])) if score > 0]
    fused = reciprocal_rank_fusion(rankings, k=config.rrf_k, limit=config.top_k_precedents,
                                   weights={source: config.source_weight(source) for source in rankings})
    by_id = {row["case_id"]: row for row in rows}
    precedents = [compact_precedent({**by_id[hit["id"]], **hit, "match_label": match_label(hit["provenance"])})
                  for hit in fused if hit["id"] in by_id]
    chunks = [compact_document(row) for row in documents]
    brief = {"query_text": query_text, "as_of_epoch": as_of, "precedents": precedents,
             "documents": chunks, "rag_config": config.model_dump(),
             "scope": "Retrieval only: free text has no authoritative trigger, assessment, or final actions."}
    packet = context_packet(brief, {}, {row["case_id"] for row in precedents}, stage="query_retrieval_only")
    return {**packet, "config": config.model_dump(), "precedents": precedents,
            "policy_chunks": chunks, "graph_paths": [], **trace,
            "warnings": [*trace["warnings"], "Free text previews use the latest dataset timestamp; graph paths require a case anchor. No policy decision is made."]}


async def preview(repo, gateway, manager, body: PreviewRequest):
    from agent.investigate import investigate
    from agent.mcp_client import GraphClient
    from agent.store import TransactionStore
    from backend.services import prepare_runtime_store

    config = load_config(repo.data / "rag_config.json")
    async with gateway.lock:
        store = await asyncio.to_thread(TransactionStore.load, repo.data / "transaction_store.pkl")
        trigger = repo.triggers().get(body.case_id) if body.case_id else None
        if body.case_id and trigger is None:
            raise HTTPException(404, "Case not found.")
        if trigger:
            prepare_runtime_store(repo, store, body.case_id)
        else:
            # Generated memory must come only from temporally verified runtime
            # triggers, not arbitrary contents of a stale pickle.
            store.case_memory = {}
        pending = manager.evidence.get(body.case_id) if trigger else None
        pending = pending if pending and pending.get("status") != "completed" else None

        async def retrieve(graph):
            readonly = ReadOnlyGraph(graph)
            if body.query_text is not None:
                return await text_preview(readonly, body.query_text.strip(), int(store.transactions.epoch.max()), config)
            _, trace = await investigate(trigger, readonly, live=True, narrator=None, preview_only=True,
                rag_config=config, mode_evidence="human" if pending else trigger.get("mode_evidence", "simulated"),
                resume_state=deepcopy(pending["snapshot"]) if pending else None,
                evidence_response=deepcopy(pending.get("response")) if pending else None)
            packet = {**trace["rag_preview"], "warnings": trace["warnings"],
                      "backend": "local" if graph.use_local else "tigergraph-mcp",
                      "embedding_indexes": trace.get("embedding_indexes", [])}
            if pending:
                packet["warnings"].append("Pending investigation preview uses its original RAG settings snapshot.")
            return packet

        failure = None
        if not gateway.local_only:
            try:
                async with gateway.client() as graph:
                    return {**await retrieve(graph), "no_llm": True, "written_to_graph": False}
            except Exception as exc:
                failure = type(exc).__name__
        async with GraphClient(use_local=True, store=store) as graph:
            result = await retrieve(graph)
        if failure:
            result["warnings"].append(f"Live graph read unavailable ({failure}); preview used the local prepared dataset.")
        return {**result, "no_llm": True, "written_to_graph": False}


def rag_router(repo, gateway, manager):
    router = APIRouter(prefix="/api/rag", tags=["GraphRAG"])
    config_lock = asyncio.Lock()

    def config_response():
        return {"config": load_config(repo.data / "rag_config.json").model_dump(),
                "defaults": RagConfig().model_dump()}

    @router.get("/config")
    async def get_config():
        return config_response()

    @router.put("/config")
    async def put_config(body: RagConfig, role: Annotated[str, Header(alias="X-Role")] = "viewer"):
        if role != "supervisor_l2":
            raise HTTPException(403, "GraphRAG settings require supervisor_l2.")
        async with config_lock:
            atomic_json(repo.data / "rag_config.json", body.model_dump())
        return config_response()

    @router.post("/preview")
    async def preview_route(body: PreviewRequest):
        if body.case_id and body.case_id not in repo.triggers():
            raise HTTPException(404, "Case not found.")
        return await preview(repo, gateway, manager, body)

    return router
