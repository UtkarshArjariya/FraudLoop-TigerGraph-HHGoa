"""Independent vector rankings and graph paths fused with RRF (k=60)."""
from __future__ import annotations

import re

from agent.embeddings import covered_ids, embed_vectors, index_lock, index_specs, provider_for


def reciprocal_rank_fusion(rankings, *, k=60, limit=None):
    if k < 1:
        raise ValueError("RRF k must be positive")
    results = {}
    for source, rows in rankings.items():
        seen = set()
        for row in rows:
            row = {"id": row} if isinstance(row, str) else row
            identifier = str(row.get("id") or row.get("case_id") or row.get("doc_id"))
            if identifier in seen:
                continue
            seen.add(identifier)
            rank = len(seen)
            result = results.setdefault(identifier, {"id": identifier, "fused_score": 0.0, "provenance": []})
            result["fused_score"] += 1 / (k + rank)
            result["provenance"].append({"source": source, "rank": rank, "paths": row.get("paths", [])})
    ordered = sorted(results.values(), key=lambda row: (-row["fused_score"], row["id"]))
    return ordered if limit is None else ordered[:limit]


def match_label(provenance):
    labels = []
    for match in provenance:
        if match["source"] == "graph":
            labels.extend(path.replace("_", " ") for path in match.get("paths", []))
        else:
            labels.append(match["source"])
    return "matched by " + " + ".join(dict.fromkeys(labels or ["analyst narrative"]))


def graph_paths(prior):
    paths = {}
    for key, label in (("direct_cases", "shared_card"), ("device_cases", "shared_device"), ("region_cases", "shared_region")):
        for row in prior.get(key, []):
            paths.setdefault(row["case_id"], []).append(label)
    for row in prior.get("cases", []):
        basis = row.get("retrieval_basis", [])
        if isinstance(basis, list):
            paths.setdefault(row["case_id"], []).extend(basis)
    return {cid: sorted(set(values)) for cid, values in paths.items()}


def sorted_vector_hits(result, *, id_key, allowed):
    distances = result.get("distances", {})
    hits = result.get("hits", result.get("documents", []))
    scored = []
    for row in hits:
        identifier = row.get(id_key)
        if identifier not in allowed or row.get("retrieval_method") in ("graph_fallback", "lexical_fallback"):
            continue
        score = row.get("similarity")
        if score is None and identifier in distances:
            score = 1 - float(distances[identifier])
        if score is not None:
            import math
            if math.isfinite(float(score)):
                scored.append((float(score), identifier))
    return [identifier for _, identifier in sorted(scored, key=lambda item: (-item[0], item[1]))]


async def retrieve_indexes(graph, query_text, as_of, library, documents, trace):
    case_ids = {row["case_id"] for row in library.get("cases", []) + library.get("memory", [])}
    documents = [row for row in documents if not str(row.get("source_id", "")).split("#", 1)[0].startswith(("CC-", "HHG-"))
                 or str(row.get("source_id", "")).split("#", 1)[0] in case_ids]
    doc_ids = {row["doc_id"] for row in documents}
    cases, docs = {}, {}
    trace["embedding_indexes"] = []
    try:
        with index_lock(graph.use_local):
            for spec in index_specs(graph.use_local):
                covered = covered_ids(spec, graph.use_local)
                if graph.use_local and getattr(graph, "store", None) is not None:
                    covered |= set(getattr(graph.store, "embedding_indexes", {}).get(spec.attribute, {}))
                if not covered:
                    continue  # No vectors to query; do not download a model for an empty index.
                detail = {"index": spec.key, "model": spec.model_id, "covered_ids": len(covered)}
                trace["embedding_indexes"].append(detail)
                try:
                    provider = provider_for(spec.model_id)
                    if provider.model_id != spec.model_id:
                        raise ValueError("Query provider differs from the index model")
                    query = (await embed_vectors(provider, [query_text], query=True))[0]
                    for name, allowed, key, target in (
                        ("similar_cases", covered & case_ids, "case_id", cases),
                        ("search_documents", covered & doc_ids, "doc_id", docs),
                    ):
                        if not allowed:
                            continue
                        result = await graph.call(name, {"query_vec": query, "as_of": as_of, "k": 30,
                            "embed_model": spec.model_id, "attribute": spec.attribute, "covered_ids": sorted(allowed)})
                        target[spec.key] = sorted_vector_hits(result, id_key=key, allowed=allowed)
                    detail["queried"] = True
                except Exception as exc:
                    detail["error"] = type(exc).__name__
                    trace["warnings"].append(f"{spec.key} retrieval unavailable ({type(exc).__name__}); other indexes and graph paths remain available.")
    except Exception as exc:
        trace["warnings"].append(f"Vector indexes unavailable ({type(exc).__name__}); using graph paths.")
    # Policy text remains available even when every vector service is unavailable.
    tokens = set(re.findall(r"\w+", query_text.lower()))
    lexical = sorted(documents, key=lambda row: (-len(tokens & set(re.findall(r"\w+", row.get("text_data", "").lower()))), row["doc_id"]))
    docs["policy text"] = [row["doc_id"] for row in lexical]
    fused = reciprocal_rank_fusion(docs)
    by_id = {row["doc_id"]: row for row in documents}
    ranked_docs = [{**by_id[row["id"]], "fused_score": row["fused_score"],
                    "provenance": row["provenance"], "match_label": match_label(row["provenance"])} for row in fused]
    trace["document_retrieval"] = fused
    return cases, ranked_docs
