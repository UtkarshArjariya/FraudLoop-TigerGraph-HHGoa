"""Prioritize documents and linked cases, then ingest resumable 20-text batches.

EMBED_LIMIT caps the deterministic target corpus, including documents. Cache
paid vectors before upload; checkpoint IDs only after successful persistence.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re

from dotenv import load_dotenv
import numpy as np

from agent.embeddings import (GeminiEmbedder, LocalGemmaEmbedder, ModelMismatch, provider_for,
    index_lock, register_index, selected_specs, checkpoint_path, covered_ids, CACHE_DIR,
    embed_vectors)
from agent.model_registry import discover
from agent.service_errors import exception_message, status_code, retry_delay
from agent.mcp_client import GraphClient
from agent.store import TransactionStore

STATUS_PATH = Path("data/embeddings/status.json")
BATCH_DELAY = 4
MAX_TEXT_CHARS = 2000


@dataclass(frozen=True)
class EmbedRow:
    vertex_type: str
    vertex_id: str
    text: str
    attributes: dict
    priority: str


def stratified(rows: list[dict]) -> list[dict]:
    """Stable round-robin sampling preserves rare pattern/outcome strata."""
    groups = defaultdict(deque)
    for row in sorted(rows, key=lambda r: r["case_id"]):
        groups[(row["pattern"], row["outcome"])].append(row)
    ordered = []
    while groups:
        for key in sorted(list(groups)):
            ordered.append(groups[key].popleft())
            if not groups[key]:
                del groups[key]
    return ordered


def prioritized_rows(store: TransactionStore, docs: list[dict], limit: int) -> list[EmbedRow]:
    """Link history to the benchmark's actual flagged entities, without labels."""
    if limit < 1:
        raise ValueError("EMBED_LIMIT must be positive")
    pack, txns = store.case_pack, store.transactions
    flags = txns[txns.txn_id.isin(set(pack.flagged_txn_id))]
    cards, customers = set(pack.card_id), set(pack.customer_id)
    devices, regions = set(flags.device_profile) - \
        {""}, set(flags.region) - {""}
    device_txns = set(txns.loc[txns.device_profile.isin(devices), "txn_id"])
    region_txns = set(txns.loc[txns.region.isin(regions), "txn_id"])
    linked, remaining = defaultdict(list), []
    for row in store.history.to_dict(orient="records"):
        ids = set(str(row["txn_ids"]).split("|"))
        score = (8 * (row["card_id"] in cards) + 4 * (row["customer_id"] in customers)
                 + 2 * bool(ids & device_txns) + bool(ids & region_txns))
        (linked[score] if score else remaining).append(row)
    selected = [EmbedRow("DocChunk", d["doc_id"], d["text_data"],
                         {"source_id": d["source_id"], "text_data": d["text_data"]}, "document") for d in docs]
    for score in sorted(linked, reverse=True):
        selected.extend(EmbedRow("ClosedCase", r["case_id"], r["analyst_notes"], {}, "benchmark_link")
                        for r in stratified(linked[score]))
    selected.extend(EmbedRow("ClosedCase", r["case_id"], r["analyst_notes"], {}, "stratified_sample")
                    for r in stratified(remaining))
    return selected[:limit]


def report_error(exc: Exception, ids: list[str], status: dict):
    message = exception_message(exc)
    status["errors"].append({"ids": ids, "error": message})
    print(f"Embedding failure for {', '.join(ids)}: {message}", flush=True)


async def request_vectors(rows: list[EmbedRow], status: dict, provider) -> list[list[float]]:
    try:
        vectors = await embed_vectors(provider, [row.text for row in rows], wait_for_quota=True)
        if not all(valid_vector(v) for v in vectors) or len(vectors) != len(rows):
            raise ValueError("Expected one finite unit-length 768-dimensional vector per text")
        return vectors
    except Exception as exc:
        report_error(exc, [row.vertex_id for row in rows], status)
        raise


async def generate_batches(rows: list[EmbedRow], status: dict, provider=None):
    """Yield singleton successes immediately when a 400 isolates invalid text."""
    try:
        vectors = await request_vectors(rows, status, provider)
    except Exception as exc:
        if status_code(exc) != 400 or "RESOURCE_EXHAUSTED" in str(exc).upper():
            raise
        print(
            "HTTP 400: retrying one text at a time (maximum 2000 characters).", flush=True)
        for row in rows:
            await asyncio.sleep(BATCH_DELAY)
            try:
                vector = (await request_vectors([row], status, provider))[0]
            except Exception as single_error:
                if status_code(single_error) != 400 or "RESOURCE_EXHAUSTED" in str(single_error).upper():
                    raise
                status["skipped_ids"].append(row.vertex_id)
                print(
                    f"Skipping rejected text {row.vertex_id}; graph retrieval remains available.", flush=True)
                continue
            yield [(row, vector)]
    else:
        yield list(zip(rows, vectors))


def valid_vector(value) -> bool:
    try:
        vector = np.asarray(value, dtype=float)
        return bool(vector.shape == (768,) and np.isfinite(vector).all()
                    and np.isclose(np.linalg.norm(vector), 1, atol=1e-4))
    except (TypeError, ValueError):
        return False


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as handle:
        json.dump(value, handle, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def completed_ids(path: Path) -> set[str]:
    return {line.strip() for line in path.read_text().splitlines() if line.strip()} if path.exists() else set()


def checkpoint(path: Path, ids: list[str], completed: set[str]):
    updated = completed | set(ids)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as handle:
        handle.write("".join(value + "\n" for value in sorted(updated)))
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
    completed.update(updated)


def documents(source: Path) -> list[dict]:
    readme = source.read_text()
    policy = readme.split("# Fraud Policy\n", 1)[
        1].split("# Answer Format\n", 1)[0]
    patterns = readme.split("## The five known fraud patterns\n", 1)[
        1].split("## Regulatory references\n", 1)[0]
    chunks = [{"doc_id": "dataset-known-patterns",
               "source_id": "HHGOA_IEEE/README.md#the-five-known-fraud-patterns", "text_data": patterns.strip()}]
    for i, part in enumerate(re.split(r"(?=^### )", policy, flags=re.M)):
        if part.strip():
            chunks.append({"doc_id": f"dataset-policy-{i:02}",
                          "source_id": "HHGOA_IEEE/README.md#fraud-policy", "text_data": part.strip()})
    # User-supplied downloaded regulatory text can be added without claiming
    # to have fetched or read inaccessible public PDFs.
    for path in sorted(Path("data/docs").glob("*.txt")):
        text = path.read_text()
        for i in range(0, len(text), 4000):
            chunks.append({"doc_id": f"regulatory-{path.stem}-{i//4000}",
                          "source_id": str(path), "text_data": text[i:i+4000]})
    return chunks


async def ingest_index(args, spec, store, graph=None):
    local = bool(args.local) or os.getenv("USE_LOCAL", "0") == "1"
    total = len(store.history) + len(store.documents)
    limit = args.limit if args.limit is not None else (total if spec.provider != "gemini" else int(os.getenv("EMBED_LIMIT", "1500")))
    rows = prioritized_rows(store, store.documents, limit)
    ids_path = checkpoint_path(spec, local)
    completed = covered_ids(spec, local)
    cache_path = CACHE_DIR / (spec.model_id.replace("/", "--") + "-768.json")
    cached = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    cached = {key: value for key, value in cached.items() if valid_vector(value)}
    status = {"model": spec.model_id, "index": spec.key, "attribute": spec.attribute,
              "dimensions": 768, "limit": limit, "total": total, "selected": len(rows),
              "checkpoint": str(ids_path), "errors": [], "skipped_ids": [], "truncated_ids": [],
              "aborted": False, "newly_completed": 0, "tool_calls": 0,
              "completed_selected": sum(row.vertex_id in completed for row in rows)}
    print(f"{spec.key}: {status['completed_selected']}/{len(rows)} selected already checkpointed; corpus {total}", flush=True)
    if args.dry_run:
        return status
    register_index(spec, local, total=total, state="building")
    # Import old Gemini checkpoint IDs without altering the original file.
    checkpoint(ids_path, [], completed)
    provider = provider_for(spec.model_id)
    start_calls = graph.tool_calls

    async def commit(pairs):
        cached.update({row.vertex_id: vector for row, vector in pairs})
        atomic_json(cache_path, cached)  # Paid results survive graph upload failures.
        for vertex_type in dict.fromkeys(row.vertex_type for row, _ in pairs):
            group = [(row, vec) for row, vec in pairs if row.vertex_type == vertex_type]
            payload = [{"vertex_id": row.vertex_id, "vector": vector,
                        "model_id": spec.model_id, "attributes": row.attributes} for row, vector in group]
            result = await graph.upsert_vectors(vertex_type, payload, attribute=spec.attribute)
            if result.get("success_count") != len(payload):
                raise RuntimeError("Incomplete vector upload; checkpoint not advanced")
            # Persist local vectors BEFORE recording them as uploaded.
            if local:
                store.save(args.store)
            checkpoint(ids_path, [row.vertex_id for row, _ in group], completed)
            status["newly_completed"] += len(group)

    try:
        for doc in store.documents:
            if doc["doc_id"] not in completed:
                await graph.call("write_document", doc)
        pending = []
        for row in rows:
            if row.vertex_id in completed:
                continue
            text = str(row.text or "").strip()
            if not text:
                status["skipped_ids"].append(row.vertex_id)
                continue
            if len(text) > MAX_TEXT_CHARS:
                status["truncated_ids"].append(row.vertex_id)
            pending.append(EmbedRow(row.vertex_type, row.vertex_id, text[:MAX_TEXT_CHARS], row.attributes, row.priority))
        # Gemini 2 multimodal semantics require singleton requests/checkpoints.
        batch_size = 1 if spec.provider == "gemini" and spec.key != "gem001" else args.batch_size
        first_request = True
        for vertex_type in ("DocChunk", "ClosedCase"):
            typed = [r for r in pending if r.vertex_type == vertex_type]
            for offset in range(0, len(typed), batch_size):
                batch = typed[offset:offset + batch_size]
                ready = [(r, cached[r.vertex_id]) for r in batch if r.vertex_id in cached]
                if ready:
                    await commit(ready)
                missing = [r for r in batch if r.vertex_id not in cached]
                if not missing:
                    continue
                if not first_request and isinstance(provider, GeminiEmbedder):
                    await asyncio.sleep(BATCH_DELAY)
                first_request = False
                async for pairs in generate_batches(missing, status, provider):
                    await commit(pairs)
    except Exception as exc:
        status["aborted"] = True
        message = exception_message(exc)
        print(f"{spec.key} stopped; other indexes remain usable: {message}", flush=True)
        if not status["errors"] or status["errors"][-1]["error"] != message:
            status["errors"].append({"ids": [], "error": message})
    finally:
        status["completed_selected"] = sum(r.vertex_id in completed for r in rows)
        status["vectors"] = len(completed)
        status["tool_calls"] = graph.tool_calls - start_calls
        status["written_to_graph"] = not local and bool(completed)
        pending_ids = [r.vertex_id for r in rows if r.vertex_id not in completed and r.vertex_id not in status["skipped_ids"]]
        register_index(spec, local, state="building" if pending_ids else "ready", total=total)
        if local:
            store.save(args.store)
        atomic_json(CACHE_DIR / (spec.key + (".local" if local else "") + ".status.json"), status)
    return status


async def run(args):
    load_dotenv()
    if not 1 <= args.batch_size <= 20:
        raise ValueError("batch-size must be between 1 and 20")
    local = bool(args.local) or os.getenv("USE_LOCAL", "0") == "1"
    mode = args.provider or os.getenv("EMBED_PROVIDER", "all")
    mode = {"auto": "all", "local": "bge", "gemini": "gem001"}.get(mode, mode)
    # Discovery is skipped in dry runs, which are strictly offline.
    registry = None if args.dry_run else await asyncio.to_thread(discover, refresh=args.refresh_models)
    if args.dry_run:
        try:
            registry = json.loads(Path(os.getenv("MODEL_REGISTRY_PATH", "data/model_registry.json")).read_text())
        except (OSError, ValueError):
            registry = {"embed": {"models": ["gemini-embedding-001", "gemini-embedding-2"]}}
    specs = selected_specs(mode, local, registry=registry)
    if not specs:
        raise ModelMismatch("No embedding model discovered for this provider; use --refresh-models or --provider bge")
    store = TransactionStore.load(args.store)
    store.documents = documents(args.source)
    results = []
    if args.dry_run:
        for spec in specs:
            results.append(await ingest_index(args, spec, store))
    else:
        with index_lock(local, exclusive=True):
            async with GraphClient(use_local=local, store=store) as graph:
                for spec in specs:
                    result = await ingest_index(args, spec, store, graph)
                    results.append(result)
                    if spec.provider == "gemma" and result["aborted"] and mode == "gemma":
                        # A fallback always writes to BGE's own index.
                        bge = selected_specs("bge", local, registry=registry)[0]
                        print("EmbeddingGemma unavailable; continuing in the separate BGE index.", flush=True)
                        results.append(await ingest_index(args, bge, store, graph))
        atomic_json(STATUS_PATH, {"indexes": results})
    return {"indexes": results, "aborted": any(r["aborted"] for r in results)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("HHGOA_IEEE/README.md"))
    parser.add_argument("--store", type=Path, default=Path("data/transaction_store.pkl"))
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--limit", type=int, help="Per-model target size; defaults to EMBED_LIMIT for Gemini and the full corpus locally")
    parser.add_argument("--dry-run", action="store_true", help="No API, graph, or file writes")
    parser.add_argument("--local", action="store_true", help="Use a separate local graph backend/checkpoint")
    parser.add_argument("--provider", choices=("gemma", "bge", "gem001", "gem2", "all"))
    parser.add_argument("--refresh-models", action="store_true")
    args = parser.parse_args()
    result = asyncio.run(run(args))
    if result["aborted"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
