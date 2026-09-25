"""Independent vector rankings and graph paths fused with RRF (k=60)."""
from __future__ import annotations

import re
import math
from datetime import datetime, timezone

from agent.embeddings import covered_ids, embed_vectors, index_lock, index_specs, provider_for
from agent.rag import settings


def reciprocal_rank_fusion(rankings, *, k=60, limit=None, weights=None):
    if k < 1:
        raise ValueError("RRF k must be positive")
    results = {}
    for source, rows in rankings.items():
        weight = (weights or {}).get(source, 1.0)
        if not math.isfinite(weight) or weight < 0:
            raise ValueError("RRF weights must be finite and nonnegative")
        if not weight:
            continue
        seen = set()
        for row in rows:
            row = {"id": row} if isinstance(row, str) else row
            identifier = str(row.get("id") or row.get("case_id") or row.get("doc_id"))
            if identifier in seen:
                continue
            seen.add(identifier)
            rank = len(seen)
            result = results.setdefault(identifier, {"id": identifier, "fused_score": 0.0, "provenance": []})
            contribution = weight / (k + rank)
            result["fused_score"] += contribution
            result["provenance"].append({"source": source, "rank": rank, "weight": weight,
                "contribution": contribution, "paths": row.get("paths", []),
                **({"similarity": row["similarity"]} if "similarity" in row else {})})
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


PATH_HOPS = {"shared_card": 1, "shared_device": 3, "shared_region": 4}
PATH_ROUTES = {"shared_card": "Card → ClosedCase",
               "shared_device": "Card → DeviceProfile → Txn → ClosedCase",
               "shared_region": "Card → Txn → BillingRegion → Txn → ClosedCase"}


def graph_paths(prior, *, config=None):
    config = settings(config)
    paths = {}
    for key, label in (("direct_cases", "shared_card"), ("device_cases", "shared_device"), ("region_cases", "shared_region")):
        for row in prior.get(key, []):
            paths.setdefault(row["case_id"], []).append(label)
    for row in prior.get("cases", []):
        basis = row.get("retrieval_basis", [])
        if isinstance(basis, list):
            paths.setdefault(row["case_id"], []).extend(basis)
    return {cid: sorted({value for value in values if PATH_HOPS.get(value, 99) <= config.graph_hop_depth})
            for cid, values in paths.items() if config.graph_similarity_weight > 0}


def describe_graph_paths(precedents):
    """Describe verified relationship classes; never invent intermediate IDs."""
    return sorted({f"{PATH_ROUTES[path]} ({PATH_HOPS[path]} hops): {row.get('case_id', row.get('id'))}; {path}"
        for row in precedents for source in row.get("provenance", []) if source["source"] == "graph"
        for path in source.get("paths", []) if path in PATH_HOPS})


def _epoch(value):
    if isinstance(value, (int, float)):
        return float(value)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()


def eligible_library(library, as_of, config=None):
    config = settings(config)
    earliest = as_of - config.time_window_days * 86400
    def eligible(row, memory=False):
        value = row.get("opened_epoch", row.get("opened_at")) if memory else row.get("closed_at")
        if value is None:
            # Native case_library already enforces as_of. Minimal compatibility
            # callers may omit timestamps; production rows always contain them.
            return True
        try:
            stamp = _epoch(value)
            return earliest <= stamp and (stamp < as_of if memory else stamp <= as_of)
        except (ValueError, TypeError, OverflowError):
            return False
    return {"cases": [row for row in library.get("cases", []) if eligible(row)],
            "memory": [row for row in library.get("memory", []) if eligible(row, True)]}


def sorted_vector_hits(result, *, id_key, allowed, min_similarity=-1.0, with_scores=False):
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
            if math.isfinite(float(score)):
                score = max(-1.0, min(1.0, float(score)))
                if score >= min_similarity:
                    scored.append((score, identifier))
    ordered = sorted(scored, key=lambda item: (-item[0], item[1]))
    return [{"id": identifier, "similarity": score} for score, identifier in ordered] if with_scores else [identifier for _, identifier in ordered]


async def retrieve_indexes(graph, query_text, as_of, library, documents, trace, *, config=None):
    config = settings(config)
    library = eligible_library(library, as_of, config)
    case_ids = {row["case_id"] for row in library.get("cases", []) + library.get("memory", [])}
    documents = [row for row in documents if not str(row.get("source_id", "")).split("#", 1)[0].startswith(("CC-", "HHG-", "CASE-"))
                 or str(row.get("source_id", "")).split("#", 1)[0] in case_ids]
    doc_ids = {row["doc_id"] for row in documents}
    cases, docs = {}, {}
    trace["embedding_indexes"] = []
    try:
        with index_lock(graph.use_local):
            for spec in index_specs(graph.use_local):
                if not config.index_enabled(spec.key):
                    continue
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
                        limit = config.top_k_precedents if name == "similar_cases" else config.top_k_policy_chunks
                        result = await graph.call(name, {"query_vec": query, "as_of": as_of, "k": min(100, max(30, limit * 3)),
                            "embed_model": spec.model_id, "attribute": spec.attribute, "covered_ids": sorted(allowed)})
                        target[spec.key] = sorted_vector_hits(result, id_key=key, allowed=allowed,
                            min_similarity=config.min_similarity, with_scores=True)
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
    fused = reciprocal_rank_fusion(docs, k=config.rrf_k, limit=config.top_k_policy_chunks,
                                   weights={source: config.source_weight(source) for source in docs})
    by_id = {row["doc_id"]: row for row in documents}
    ranked_docs = [{**by_id[row["id"]], "fused_score": row["fused_score"],
                    "provenance": row["provenance"], "match_label": match_label(row["provenance"])} for row in fused]
    trace["document_retrieval"] = fused
    return cases, ranked_docs
