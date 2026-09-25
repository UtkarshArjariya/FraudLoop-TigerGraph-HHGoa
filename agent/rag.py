"""Validated retrieval settings and the shared, compact narration context.

Retrieval controls never change the encoded bank policy or turn generated case
conclusions into confirmed historical outcomes. Context contains no vectors.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class IndexSwitches(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    bge: bool = True
    gemma: bool = True
    gem001: bool = True
    gem2: bool = True


class IndexWeights(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    bge: float = Field(1.0, ge=0, le=5)
    gemma: float = Field(1.0, ge=0, le=5)
    gem001: float = Field(1.0, ge=0, le=5)
    gem2: float = Field(1.0, ge=0, le=5)


class RagConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    enabled_indexes: IndexSwitches = Field(default_factory=IndexSwitches)
    rrf_weights: IndexWeights = Field(default_factory=IndexWeights)
    rrf_k: int = Field(60, ge=1, le=200)
    top_k_precedents: int = Field(5, ge=1, le=20)
    top_k_policy_chunks: int = Field(12, ge=1, le=30)
    graph_hop_depth: int = Field(4, ge=0, le=4)
    time_window_days: int = Field(365, ge=1, le=3650)
    min_similarity: float = Field(-1.0, ge=-1, le=1)
    graph_similarity_weight: float = Field(1.0, ge=0, le=5)

    def source_weight(self, source: str) -> float:
        family = "gem2" if source.startswith("gem2_") else source
        if family in type(self.enabled_indexes).model_fields:
            return getattr(self.rrf_weights, family) if getattr(self.enabled_indexes, family) else 0.0
        return self.graph_similarity_weight if source == "graph" else 1.0

    def index_enabled(self, source: str) -> bool:
        family = "gem2" if source.startswith("gem2_") else source
        return family in type(self.enabled_indexes).model_fields and self.source_weight(source) > 0


def settings(value: RagConfig | dict | None = None) -> RagConfig:
    return value.model_copy(deep=True) if isinstance(value, RagConfig) else RagConfig.model_validate(value or {})


def load_config(path: Path | str = "data/rag_config.json") -> RagConfig:
    path = Path(path)
    # Corrupt settings fail visibly rather than silently claiming another config.
    return settings(json.loads(path.read_text())) if path.exists() else RagConfig()


def compact_document(row: dict, *, excerpt_limit: int = 1200) -> dict:
    text = str(row.get("text_data", row.get("text", "")))
    return {**{key: row[key] for key in ("doc_id", "source_id", "title", "fused_score", "provenance", "match_label") if key in row},
            "text_data": text[:excerpt_limit], "text_truncated": len(text) > excerpt_limit}


def compact_precedent(row: dict) -> dict:
    keys = ("case_id", "outcome", "pattern", "closed_at", "fused_score", "provenance", "match_label", "retrieval_basis")
    notes = str(row.get("analyst_notes", row.get("summary", "")))
    return {**{key: row[key] for key in keys if key in row}, "analyst_notes": notes[:600],
            "notes_truncated": len(notes) > 600,
            "source_kind": "confirmed_history" if "outcome" in row else "agent_conclusion"}


def build_evidence_brief(case, result, documents, requests, initial_actions, final_actions, config):
    """The exact structured brief used by both previews and live narration."""
    # Free-form human notes remain attributable in the saved answer but cannot
    # introduce unverified claims or instructions into guarded narration.
    safe_requests = [{key: value for key, value in request.items() if key != "note"} for request in requests]
    return {"case_id": case["case_id"], "customer_id": case["customer_id"], "card_id": case["card_id"],
        "trigger_type": case.get("trigger_type", ""), "as_of": case.get("opened_at", ""),
        "verdict": result.verdict, "probability": result.probability, "pattern": result.pattern,
        "exposure_usd": result.exposure, "evidence": result.evidence, "requests": safe_requests,
        "episode": [{key: row.get(key) for key in ("txn_id", "ts", "amount", "channel", "region", "card_id", "customer_id", "device_profile")} for row in result.episode],
        "initial_actions": initial_actions, "final_actions": final_actions,
        "precedents": [compact_precedent(row) for row in result.precedents],
        "documents": [compact_document(row) for row in documents], "rag_config": settings(config).model_dump()}


def allowed_entity_ids(case, result):
    values = {str(row[key]) for row in result.episode for key in ("txn_id", "card_id", "customer_id", "device_profile") if row.get(key)}
    values |= {entity for item in result.evidence for entity in item["entity_ids"]}
    return values | {case["case_id"], case["card_id"], case["customer_id"]}


def narration_contents(brief: dict, fallback: dict, allowed_ids: set[str]) -> str:
    return json.dumps({"evidence_brief": brief, "fallback_templates": fallback,
                       "allowed_entity_ids": sorted(allowed_ids)}, ensure_ascii=False,
                      sort_keys=True, separators=(",", ":"), allow_nan=False)


def token_estimate(contents: str, system_instruction: str = "") -> int:
    """A disclosed approximation; it never calls a model's token-count API."""
    return math.ceil((len(contents) + len(system_instruction)) / 4)


def context_packet(brief, fallback, allowed_ids, *, stage="final"):
    from agent.llm import SYSTEM_PROMPT
    contents = narration_contents(brief, fallback, allowed_ids)
    return {"evidence_brief": brief, "llm_context": contents, "system_instruction": SYSTEM_PROMPT,
            "estimated_tokens": token_estimate(contents, SYSTEM_PROMPT),
            "token_estimate_method": "ceil((context characters + system characters) / 4); not a model token count",
            "stage": stage}
