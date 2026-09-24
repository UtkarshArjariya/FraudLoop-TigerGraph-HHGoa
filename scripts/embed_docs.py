"""Ingest policy/pattern text and embed closed-case memory in resumable batches.

No key: store the original text and explicitly use lexical retrieval. A vector
is never substituted with random, hashed, or zero-filled values.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import re

from agent.embeddings import configured, embed_batch
from agent.mcp_client import GraphClient
from agent.store import TransactionStore


def documents(source: Path) -> list[dict]:
    readme = source.read_text()
    policy = readme.split("# Fraud Policy\n", 1)[1].split("# Answer Format\n", 1)[0]
    patterns = readme.split("## The five known fraud patterns\n", 1)[1].split("## Regulatory references\n", 1)[0]
    chunks = [{"doc_id": "dataset-known-patterns", "source_id": "HHGOA_IEEE/README.md#the-five-known-fraud-patterns", "text_data": patterns.strip()}]
    for i, part in enumerate(re.split(r"(?=^### )", policy, flags=re.M)):
        if part.strip():
            chunks.append({"doc_id": f"dataset-policy-{i:02}", "source_id": "HHGOA_IEEE/README.md#fraud-policy", "text_data": part.strip()})
    # User-supplied downloaded regulatory text can be added without claiming
    # to have fetched or read inaccessible public PDFs.
    for path in sorted(Path("data/docs").glob("*.txt")):
        text = path.read_text()
        for i in range(0, len(text), 4000):
            chunks.append({"doc_id": f"regulatory-{path.stem}-{i//4000}", "source_id": str(path), "text_data": text[i:i+4000]})
    return chunks


async def run(args):
    store = TransactionStore.load(args.store)
    store.documents = documents(args.source)
    rows = [("ClosedCase", r.case_id, r.analyst_notes, {}) for r in store.history.itertuples(index=False)]
    rows += [("DocChunk", d["doc_id"], d["text_data"], {"source_id": d["source_id"], "text_data": d["text_data"]}) for d in store.documents]
    cache_path = Path("data/embeddings/gemini-embedding-001-768.json")
    cached = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    store.embeddings.update(cached)
    status = {"model": "gemini-embedding-001", "dimensions": 768, "documents": len(store.documents),
              "closed_cases": len(store.history), "vectors": len(cached), "retrieval": "lexical", "errors": []}
    if configured():
        missing = [row for row in rows if row[1] not in cached]
        for offset in range(0, len(missing), args.batch_size):
            batch = missing[offset:offset+args.batch_size]
            try:
                vectors = await asyncio.to_thread(embed_batch, [r[2] for r in batch])
            except Exception as exc:
                status["errors"].append(type(exc).__name__)
                print(f"Embedding batch failed twice ({type(exc).__name__}); remaining text stays searchable.")
                break
            cached.update({r[1]: v for r, v in zip(batch, vectors)})
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(cached))
            print(f"Embedded {len(cached)}/{len(rows)} texts", flush=True)
        store.embeddings.update(cached)
    else:
        print("Gemini not configured: ingesting original text; lexical retrieval enabled.", flush=True)
    async with GraphClient(use_local=args.local, store=store) as graph:
        if not args.local:
            for doc in store.documents:
                await graph.call("write_document", doc)
        for vertex_type in ["ClosedCase", "DocChunk"]:
            vectors = [{"vertex_id": row[1], "vector": cached[row[1]], "attributes": row[3]}
                       for row in rows if row[0] == vertex_type and row[1] in cached]
            for offset in range(0, len(vectors), args.batch_size):
                await graph.upsert_vectors(vertex_type, vectors[offset:offset+args.batch_size])
        status["tool_calls"] = graph.tool_calls
    status["vectors"] = len(cached)
    status["retrieval"] = "vector+graph" if cached else "lexical+graph"
    status["written_to_graph"] = not args.local
    store.save(args.store)
    Path("data/embeddings").mkdir(parents=True, exist_ok=True)
    Path("data/embeddings/status.json").write_text(json.dumps(status, indent=2) + "\n")
    print(json.dumps(status, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("HHGOA_IEEE/README.md"))
    parser.add_argument("--store", type=Path, default=Path("data/transaction_store.pkl"))
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--local", action="store_true")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
