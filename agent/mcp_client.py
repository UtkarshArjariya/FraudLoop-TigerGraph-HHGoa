"""The sole runtime gateway to TigerGraph, using the official stdio MCP server.

Database provisioning uses Docker/GSQL separately. Investigation code never
calls pyTigerGraph or HTTP directly. USE_LOCAL=1 explicitly selects the fallback.
"""
from __future__ import annotations

from agent.embeddings import require_index, normalize as normalize_vectors, ModelMismatch

import asyncio
from contextlib import AsyncExitStack
import json
import math
import os
from pathlib import Path
import re
import sys
from typing import Any

from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agent.store import TransactionStore


class GraphError(RuntimeError):
    pass


def decode_response(result: Any) -> dict:
    """tigergraph-mcp 1.0.3 wraps JSON in a fence plus duplicated prose."""
    texts = [block.text for block in result.content if getattr(block, "type", None) == "text"]
    for text in texts:
        fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
        candidate = fenced.group(1) if fenced else text
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if getattr(result, "isError", False) or payload.get("success") is False:
            # No connection configuration or credentials in exception messages.
            error = payload.get("error", {})
            message = error.get("message", "MCP operation failed") if isinstance(error, dict) else str(error)
            raise GraphError(message[:500])
        return payload.get("data", payload)
    raise GraphError("MCP response did not contain a valid JSON envelope")


def normalize(value: Any) -> Any:
    if isinstance(value, list):
        return [normalize(item) for item in value]
    if isinstance(value, dict):
        if "v_id" in value and "attributes" in value:
            attrs = dict(value["attributes"])
            primary = {"Txn": "txn_id", "ClosedCase": "case_id", "FraudCase": "case_id", "DocChunk": "doc_id"}.get(value.get("v_type"))
            if primary:
                attrs.setdefault(primary, str(value["v_id"]))
            return normalize(attrs)
        return {key: normalize(item) for key, item in value.items()}
    return value


def vector_similarities(result: dict) -> dict[str, float]:
    """Use actual cosine/distance values, never unordered GSQL set positions."""
    distances = result.get("distances", {})
    scores = {}
    for row in result.get("hits", []):
        cid = row.get("case_id")
        if not cid or row.get("retrieval_method") in ("graph_fallback", "lexical_fallback"):
            continue
        similarity = None
        if "similarity" in row:
            similarity = float(row["similarity"])
        elif isinstance(distances, dict) and cid in distances:
            similarity = 1 - float(distances[cid])
        if similarity is not None and math.isfinite(similarity):
            # HNSW float rounding can give a tiny negative cosine distance.
            scores[cid] = min(1.0, max(-1.0, similarity))
    return scores


class GraphClient:
    def __init__(self, *, use_local: bool | None = None, store: TransactionStore | None = None):
        load_dotenv()
        self.use_local = os.getenv("USE_LOCAL", "0") == "1" if use_local is None else use_local
        self.store = store
        self.graph_name = os.getenv("TG_GRAPHNAME", "FraudGraph")
        self.tool_calls = 0
        self.call_log: list[dict] = []
        self.session: ClientSession | None = None
        self.stack = AsyncExitStack()
        self.tool_names: set[str] = set()
        self.backend = None

    async def __aenter__(self):
        if self.use_local:
            from agent.local_backend import LocalBackend
            self.store = self.store or TransactionStore.load()
            self.backend = LocalBackend(self.store)
            return self
        env = os.environ.copy()
        # Do not forward the Gemini secret to a graph-only subprocess.
        env.pop("GEMINI_API_KEY", None)
        env.pop("GEMINI_EMBEDDING", None)
        server = StdioServerParameters(command=sys.executable,
                                       args=["-m", "tigergraph_mcp.main"], env=env)
        Path("data/runs").mkdir(parents=True, exist_ok=True)
        self.errlog = self.stack.enter_context(open("data/runs/mcp-stderr.log", "a"))
        try:
            read, write = await self.stack.enter_async_context(stdio_client(server, errlog=self.errlog))
            self.session = await self.stack.enter_async_context(ClientSession(read, write))
            await asyncio.wait_for(self.session.initialize(), timeout=45)
            inventory = await asyncio.wait_for(self.session.list_tools(), timeout=30)
            self.tool_names = {tool.name for tool in inventory.tools}
            self._tool("run_installed_query")
        except BaseException:
            await self.stack.aclose()
            raise
        return self

    async def __aexit__(self, *args):
        await self.stack.aclose()

    def _tool(self, suffix: str) -> str:
        matches = [name for name in self.tool_names if name == suffix or name.endswith("__" + suffix)]
        if len(matches) != 1:
            raise GraphError(f"Expected exactly one MCP tool named {suffix}; found {len(matches)}")
        return matches[0]

    async def tool(self, suffix: str, arguments: dict) -> dict:
        if self.use_local or self.session is None:
            raise GraphError("Raw MCP tools require a connected TigerGraph backend")
        self.tool_calls += 1
        self.call_log.append({"tool": suffix, "query": arguments.get("query_name", "")})
        result = await asyncio.wait_for(self.session.call_tool(self._tool(suffix), arguments), timeout=120)
        return decode_response(result)

    async def call(self, name: str, params: dict | None = None) -> dict:
        params = params or {}
        if name in ("similar_cases", "search_documents"):
            params = dict(params)
            spec = require_index(params.pop("embed_model", ""), params.pop("attribute", ""), self.use_local)
            if not params.get("covered_ids"):
                return {"hits": [], "documents": [], "distances": {}}
            normalize_vectors([params.get("query_vec", [])], 1)
            if self.use_local:
                params["embed_model"], params["attribute"] = spec.model_id, spec.attribute
            else:
                name += "_" + spec.key
        if self.use_local:
            self.tool_calls += 1
            self.call_log.append({"tool": "local", "query": name})
            return await self.backend.call(name, params)
        data = await self.tool("run_installed_query", {
            "graph_name": self.graph_name, "query_name": name, "params": params,
        })
        result = normalize(data.get("result", data))
        if isinstance(result, list):
            merged: dict = {}
            for item in result:
                if isinstance(item, dict):
                    merged.update(item)
            if "rows" in merged:
                merged["transactions"] = merged.pop("rows")
            if "memory_cases" in merged:
                merged["memory"] = merged.pop("memory_cases")
            return merged
        return result

    async def upsert_vectors(self, vertex_type: str, vectors: list[dict], *, attribute: str = "embedding") -> dict:
        if vertex_type not in ("ClosedCase", "DocChunk", "FraudCase"):
            raise ModelMismatch("Unsupported vector vertex type")
        for row in vectors:
            require_index(row.get("model_id", row.get("attributes", {}).get("embed_model", "")), attribute, self.use_local)
            normalize_vectors([row["vector"]], 1)
        if self.use_local:
            self.tool_calls += 1
            if not hasattr(self.store, "embedding_indexes"):
                self.store.embedding_indexes = {}
            index = self.store.embedding_indexes.setdefault(attribute, {})
            for row in vectors:
                index[str(row["vertex_id"])] = row["vector"]
            return {"success_count": len(vectors), "written_to_graph": False}
        # model_id is our guard metadata, not a TigerGraph vertex attribute.
        payload = [{k: v for k, v in row.items() if k != "model_id"} for row in vectors]
        result = await self.tool("upsert_vectors", {"graph_name": self.graph_name,
            "vertex_type": vertex_type, "vector_attribute": attribute, "vectors": payload})
        if result.get("failed_count", 0) or result.get("errors") or result.get("success_count") != len(vectors):
            raise GraphError("Vector upsert incomplete; checkpoint must not advance")
        return result


async def smoke(local: bool):
    async with GraphClient(use_local=local) as client:
        if local:
            print("Local transaction store ready")
        else:
            print(f"MCP initialized: {len(client.tool_names)} tools; installed-query gateway available")


if __name__ == "__main__":
    asyncio.run(smoke("--local" in sys.argv))
