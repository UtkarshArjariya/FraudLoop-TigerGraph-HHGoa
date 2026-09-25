"""Previewed, bounded reingestion of authoritative transactions and reference text."""
from __future__ import annotations

import asyncio
from contextlib import nullcontext
import csv
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Annotated, Literal
import uuid

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from agent.embeddings import LOCAL_MODEL, ModelMismatch, checkpoint_path, completed_ids, index_lock, normalize, require_index
from backend.data import atomic_json, csv_rows, read_json, utc_now
from backend.detector import TXN_COLUMNS
from backend.stream import upsert_transaction

MAX_CSV_BYTES = 2 * 1024 * 1024
MAX_CSV_ROWS = 2000
MAX_POLICY_BYTES = 64 * 1024
CHUNK_CHARS = 1200
MAX_CHUNKS = 64
ALIASES = {"TransactionID": "txn_id", "TransactionDT": "txn_seconds", "TransactionAmt": "amount",
           "ProductCD": "product", "addr1": "region", "addr2": "country", "P_emaildomain": "p_email", "R_emaildomain": "r_email"}
NUMERIC = {"txn_seconds", "amount", "risk_score", "epoch", "D1", "dist1", "C1", "C2", "C4", "C13", "C14",
           "card1", "card2", "card3", "card5", "region", "country"}


class BgeCacheUnavailable(RuntimeError):
    """Policy text can remain staged until the operator restores local weights."""


BGE_CACHE_MESSAGE = (
    "Cached BGE weights are missing or could not be loaded. Restore the existing "
    "BAAI/bge-base-en-v1.5 FastEmbed cache (FASTEMBED_CACHE_PATH), or set "
    "FRAUDLOOP_BGE_MODEL_PATH to an existing BGE ONNX model directory, then retry commit. "
    "The upload remains staged; no model was downloaded and no new graph write was made.")


class IngestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["preview", "commit"] = "preview"
    content: str = Field(min_length=1, max_length=MAX_CSV_BYTES)
    filename: str = Field("upload", max_length=200)
    preview_id: str | None = Field(None, pattern=r"^[a-f0-9]{32}$")


def upload_digest(kind, content, filename):
    return hashlib.sha256((kind + "\0" + filename + "\0" + content).encode()).hexdigest()


def equivalent(value, expected, column):
    if value == expected:
        return True
    if column not in NUMERIC or not value or not expected:
        return False
    try:
        supplied, authoritative = Decimal(value), Decimal(expected)
        return supplied.is_finite() and authoritative.is_finite() and supplied == authoritative
    except InvalidOperation:
        return False


def transaction_preview(repo, content):
    if len(content.encode()) > MAX_CSV_BYTES:
        raise ValueError("CSV exceeds the 2 MiB limit.")
    reader = csv.DictReader(io.StringIO(content.lstrip("\ufeff")))
    headers = reader.fieldnames or []
    if not headers or len(headers) != len(set(headers)):
        raise ValueError("CSV requires a header with unique column names.")
    mapped = [ALIASES.get(name, name) for name in headers]
    if len(mapped) != len(set(mapped)):
        raise ValueError("CSV aliases duplicate the same prepared column.")
    unknown = [name for name, target in zip(headers, mapped) if target not in TXN_COLUMNS]
    if unknown:
        raise ValueError("Unsupported columns: " + ", ".join(unknown[:12]) + ". Use the prepared transaction subset.")
    if "txn_id" not in mapped:
        raise ValueError("CSV requires txn_id or TransactionID.")
    uploaded, errors = {}, []
    for position, raw in enumerate(reader, start=2):
        if position - 1 > MAX_CSV_ROWS:
            raise ValueError("CSV exceeds the 2,000-row limit.")
        if None in raw or any(value is None for value in raw.values()):
            raise ValueError(f"CSV row {position} does not match its header.")
        row = {ALIASES.get(key, key): value for key, value in raw.items()}
        txn_id = row["txn_id"]
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", txn_id):
            errors.append(f"Row {position}: invalid transaction ID.")
        elif txn_id in uploaded:
            errors.append(f"Row {position}: duplicate transaction ID {txn_id}.")
        else:
            uploaded[txn_id] = row
    if not uploaded:
        raise ValueError("CSV has no transaction rows.")
    matches = {row["txn_id"]: row for row in csv_rows(repo.data / "graph/transactions.csv") if row["txn_id"] in uploaded}
    rows = []
    for txn_id, row in uploaded.items():
        authoritative = matches.get(txn_id)
        if authoritative is None:
            errors.append(f"Unknown prepared dataset transaction ID {txn_id}.")
            continue
        changed = [key for key, value in row.items() if not equivalent(value, authoritative.get(key, ""), key)]
        if changed:
            errors.append(f"Transaction {txn_id}: values differ from the authoritative dataset in " + ", ".join(changed[:10]) + ".")
        else:
            rows.append(authoritative)
    sample = [{key: row.get(key, "") for key in ("txn_id", "amount", "card_id", "customer_id", "ts", "device_profile")}
              for row in rows[:20]]
    return {"rows": len(uploaded), "chunks": 0, "sample": sample, "errors": errors[:30], "items": rows,
            "limits": {"max_bytes": MAX_CSV_BYTES, "max_rows": MAX_CSV_ROWS},
            "disclosure": "Only existing prepared dataset IDs and unchanged supplied values can be reingested. Original CSV files, transaction store and saved answers are preserved."}


def policy_preview(content):
    if len(content.encode()) > MAX_POLICY_BYTES:
        raise ValueError("Policy text exceeds the 64 KiB limit.")
    if "\x00" in content or not content.strip():
        raise ValueError("Policy upload must contain nonempty markdown or plain text.")
    normalized = content.replace("\r\n", "\n").strip()
    digest = hashlib.sha256(normalized.encode()).hexdigest()
    chunks, current = [], ""
    for paragraph in normalized.split("\n\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if current and len(current) + len(paragraph) + 2 <= CHUNK_CHARS:
            current += "\n\n" + paragraph
            continue
        if current:
            chunks.append(current)
            current = ""
        while len(paragraph) > CHUNK_CHARS:
            split = paragraph.rfind(" ", 0, CHUNK_CHARS + 1)
            split = split if split > CHUNK_CHARS // 2 else CHUNK_CHARS
            chunks.append(paragraph[:split])
            paragraph = paragraph[split:].lstrip()
        current = paragraph
    if current:
        chunks.append(current)
    if len(chunks) > MAX_CHUNKS:
        raise ValueError("Policy text exceeds the 64-chunk limit.")
    items = [{"doc_id": f"DOC-UP-{digest[:24]}-{position:03d}", "source_id": f"upload:{digest}", "text_data": chunk}
             for position, chunk in enumerate(chunks, start=1)]
    return {"rows": 0, "chunks": len(items), "sample": items[:20], "errors": [], "items": items,
            "limits": {"max_bytes": MAX_POLICY_BYTES, "max_chunks": MAX_CHUNKS, "chunk_chars": CHUNK_CHARS},
            "disclosure": "Uploaded documents are reference material. Encoded bank policy, verdict rules and approval routes remain authoritative. Commit uses cached BGE weights and emb_bge only; no model download or LLM call is made."}


def cached_bge_directory():
    """Inspect only known cache locations; never fetch weights or walk home."""
    roots = [Path(os.getenv("FASTEMBED_CACHE_PATH", str(Path(tempfile.gettempdir()) / "fastembed_cache"))),
             Path(os.getenv("HF_HUB_CACHE") or os.getenv("HUGGINGFACE_HUB_CACHE") or
                  str(Path(os.getenv("HF_HOME", str(Path.home() / ".cache/huggingface"))) / "hub"))]
    candidates = []
    if os.getenv("FRAUDLOOP_BGE_MODEL_PATH"):
        candidates.append(Path(os.environ["FRAUDLOOP_BGE_MODEL_PATH"]).expanduser())
    for root in roots:
        candidates.append(root / "fast-bge-base-en-v1.5")
        for name in ("models--Qdrant--bge-base-en-v1.5-onnx-Q", "models--qdrant--bge-base-en-v1.5-onnx-Q"):
            candidates.extend(sorted((root / name / "snapshots").glob("*")))
    for path in candidates:
        if all((path / name).is_file() for name in ("model_optimized.onnx", "tokenizer.json", "config.json")):
            return path
    return None


def cached_bge_vectors(texts):
    from agent.embeddings import provider_for
    provider = provider_for(LOCAL_MODEL)
    if provider._model is None:
        directory = cached_bge_directory()
        if directory is None:
            raise BgeCacheUnavailable(BGE_CACHE_MESSAGE)
        from fastembed import TextEmbedding
        try:
            provider._model = TextEmbedding(model_name=LOCAL_MODEL, providers=["CPUExecutionProvider"],
                threads=2, local_files_only=True, specific_model_path=str(directory))
        except (OSError, ValueError, RuntimeError) as exc:
            raise BgeCacheUnavailable(BGE_CACHE_MESSAGE) from exc
    return normalize(provider.embed(texts), len(texts))


def record_bge_documents(ids):
    from scripts.embed_docs import checkpoint
    spec = require_index(LOCAL_MODEL, "emb_bge", False)
    path = checkpoint_path(spec, False)
    checkpoint(path, ids, completed_ids(path))


class IngestService:
    def __init__(self, repo, gateway):
        self.repo, self.gateway = repo, gateway
        self.root = repo.data / "ingestion"
        self.lock = asyncio.Lock()
        self.embed = cached_bge_vectors
        self.index_guard = lambda: index_lock(False, exclusive=True)
        self.require_index = lambda: require_index(LOCAL_MODEL, "emb_bge", False)
        self.record_vectors = record_bge_documents

    async def preview(self, kind, request):
        if kind == "transactions":
            value = await asyncio.to_thread(transaction_preview, self.repo, request.content)
        else:
            value = policy_preview(request.content)
        preview = {"preview_id": uuid.uuid4().hex, "kind": kind, "status": "preview", "filename": request.filename,
            "digest": upload_digest(kind, request.content, request.filename), "created_at": utc_now(),
            "can_commit": not value["errors"], "written_to_graph": False, **value}
        atomic_json(self.root / "previews" / f"{preview['preview_id']}.json", preview)
        return {key: value for key, value in preview.items() if key != "items"}

    async def commit(self, kind, request, role):
        if not request.preview_id:
            raise ValueError("Commit requires a successful preview_id.")
        async with self.lock:
            preview = read_json(self.root / "previews" / f"{request.preview_id}.json")
            digest = upload_digest(kind, request.content, request.filename)
            if not preview or preview["kind"] != kind or preview["digest"] != digest:
                raise ValueError("Preview identity does not match this upload; preview the exact content again.")
            if not preview["can_commit"]:
                raise ValueError("An invalid preview cannot be committed.")
            path = self.root / "receipts" / f"{request.preview_id}.json"
            previous = read_json(path, {})
            if previous.get("status") == "committed":
                return previous
            # Revalidate authoritative transaction rows immediately before any
            # graph action; a local prepared-data change invalidates the preview.
            if kind == "transactions":
                current = await asyncio.to_thread(transaction_preview, self.repo, request.content)
                if current["errors"] or current["items"] != preview["items"]:
                    raise ValueError("Authoritative data changed after preview; create a fresh preview.")
            receipt = {"receipt_id": previous.get("receipt_id") or uuid.uuid4().hex,
                "preview_id": request.preview_id, "kind": kind, "digest": digest,
                "committed_by": role, "committed_at": utc_now(), "status": "staged_locally",
                "written_to_graph": False,
                "items": [dict(item) for item in previous.get("items", []) if item.get("written_to_graph")],
                "warning": None}
            items = preview["items"]
            if len(receipt["items"]) == len(items):
                # A crash may have followed the last per-item durable receipt
                # but preceded the final promotion. No graph retry is needed.
                receipt.update(status="committed", written_to_graph=True)
                self.save(receipt, path, len(items))
                return receipt
            if self.gateway.local_only:
                return self.stage(receipt, items, path, "Local-only mode: upload is staged in its preview; no graph writes or vectors are claimed.")
            async with self.gateway.lock:
                if not await self.gateway.gse_for_write():
                    return self.stage(receipt, items, path, "GSE unavailable after its write guard; upload remains staged locally.")
                try:
                    guard = self.index_guard() if kind == "policy" else nullcontext()
                    with guard:
                        vectors = None
                        if kind == "policy":
                            self.require_index()
                            # Holding the existing nonblocking index lock keeps
                            # operator jobs and query models from racing this use.
                            vectors = await asyncio.to_thread(self.embed, [item["text_data"] for item in items])
                            vectors = normalize(vectors, len(items))
                        async with self.gateway.client() as graph:
                            completed = {item["id"] for item in receipt["items"]}
                            for position, item in enumerate(items):
                                identifier = item["txn_id"] if kind == "transactions" else item["doc_id"]
                                if identifier in completed:
                                    continue
                                result = {"id": identifier, "type": "Txn" if kind == "transactions" else "DocChunk",
                                    "status": "committed", "written_to_graph": True}
                                if identifier not in completed:
                                    if not await self.gateway.gse_for_write():
                                        raise RuntimeError("GSE unavailable before an ingestion item.")
                                    if kind == "transactions":
                                        await upsert_transaction(graph, item)
                                    else:
                                        await graph.tool("add_node", {"graph_name": graph.graph_name,
                                            "vertex_type": "DocChunk", "vertex_id": identifier,
                                            "attributes": {"source_id": item["source_id"], "text_data": item["text_data"], "embed_model": LOCAL_MODEL}})
                                        await graph.upsert_vectors("DocChunk", [{"vertex_id": identifier,
                                            "vector": vectors[position], "model_id": LOCAL_MODEL, "attributes": {}}], attribute="emb_bge")
                                        self.record_vectors([identifier])
                                if kind == "policy":
                                    result["vector_written"] = True
                                receipt["items"].append(result)
                                # Each completed item is durably recorded. A
                                # failed retry need not repeat earlier uploads.
                                self.save(receipt, path, len(items))
                except ModelMismatch as exc:
                    raise ValueError("Embedding index is busy or its ownership is unavailable; retry after the operator job finishes.") from exc
                except BgeCacheUnavailable as exc:
                    return self.stage(receipt, items, path, str(exc))
                except Exception as exc:
                    receipt["status"] = "partial" if receipt["items"] else "failed"
                    receipt["warning"] = f"{type(exc).__name__}: ingestion could not finish. Partial idempotent graph writes may exist; completed items are recorded. No model was downloaded."
                    known = {item["id"] for item in receipt["items"]}
                    receipt["items"].extend({"id": item.get("txn_id", item.get("doc_id")),
                        "type": "Txn" if kind == "transactions" else "DocChunk", "status": "failed", "written_to_graph": False}
                        for item in items if item.get("txn_id", item.get("doc_id")) not in known)
                    self.save(receipt, path, len(items))
                    return receipt
            receipt.update(status="committed", written_to_graph=True)
            self.save(receipt, path, len(items))
            return receipt

    def save(self, receipt, path, total):
        completed = sum(item.get("written_to_graph", False) for item in receipt["items"])
        staged = sum(item.get("status") == "staged_locally" for item in receipt["items"])
        receipt["counts"] = {"total": total, "completed": completed, "staged": staged,
                             "failed": sum(item.get("status") == "failed" for item in receipt["items"])}
        atomic_json(path, receipt)

    def stage(self, receipt, items, path, warning):
        receipt["warning"] = warning
        completed = {item["id"] for item in receipt["items"] if item.get("written_to_graph")}
        receipt["status"] = "partial" if completed else "staged_locally"
        receipt["items"].extend({"id": item.get("txn_id", item.get("doc_id")), "type": "Txn" if receipt["kind"] == "transactions" else "DocChunk",
                             "status": "staged_locally", "written_to_graph": False, "vector_written": False}
                            for item in items if item.get("txn_id", item.get("doc_id")) not in completed)
        self.save(receipt, path, len(items))
        return receipt


def ingest_router(service):
    router = APIRouter()

    async def handle(kind, body, role):
        if body.action == "commit" and role != "supervisor_l2":
            raise HTTPException(403, "Ingestion commit requires supervisor_l2.")
        try:
            return await service.preview(kind, body) if body.action == "preview" else await service.commit(kind, body, role)
        except (ValueError, csv.Error) as exc:
            raise HTTPException(409 if body.action == "commit" else 422, str(exc)) from exc

    @router.post("/api/ingest/transactions")
    async def transactions(body: IngestRequest, role: Annotated[str, Header(alias="X-Role")] = "viewer"):
        return await handle("transactions", body, role)

    @router.post("/api/ingest/policy")
    async def policy(body: IngestRequest, role: Annotated[str, Header(alias="X-Role")] = "viewer"):
        return await handle("policy", body, role)

    return router
