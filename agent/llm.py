"""Guarded Gemini prose generation; assessment and policy remain code-owned.

Both failed service attempts and rejected drafts fall back to supplied text.
Usage is counted before draft validation, including thinking tokens and any
usage metadata exposed by a failed SDK response. Secrets never enter metrics.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

from google.genai import types
from pydantic import BaseModel, ConfigDict, Field
from agent.models import model_chain
from agent.service_errors import rate_limited, transient, retry_delay, status_code

from agent.model_clients import configured, get_client
from agent.quota import QuotaState


TEXT_FIELDS = ("summary", "pattern_description",
               "sar_narrative", "what_changed", "stop_reason")
ENTITY_ID = re.compile(
    r"(?<![A-Za-z0-9])(?:C\d+-K\d+|CC-\d+|HHG-\d+|CASE-[A-Za-z0-9-]+|C\d+|T\d{7}|\d{7})(?![A-Za-z0-9-])")
COMPLETED_ACTION = re.compile(
    r"\b(?:cards?|accounts?|authorizations?)\b[^.!?\n]{0,60}?\b(?:was|were|is|are|has been|have been)\s+(?:now\s+)?(?:blocked|declined|frozen|reissued)\b"
    r"|\b(?:cards?|accounts?)\s+(?:C\d+-K\d+\s+)?(?:blocked|reissued)\b"
    r"|\b(?:SAR|report|filing)\b[^.!?\n]{0,60}?\b(?:was|were|is|has been|have been)\s+(?:filed|submitted|sent)\b"
    r"|\b(?:customer|cardholder)\b[^.!?\n]{0,50}?\b(?:was|has been)\s+(?:contacted|called|emailed)\b"
    r"|\b(?:we|the agent|the bank|the investigator)\s+(?:have\s+|has\s+)?(?:blocked|filed|submitted|contacted|called|emailed)\b"
    r"|\b(?:filed|submitted)\s+(?:a\s+|the\s+)?(?:SAR|regulatory report)\b",
    re.I,
)


class NarrativeDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str
    pattern_description: str
    sar_narrative: str
    what_changed: str
    stop_reason: str
    cited_entity_ids: list[str] = Field(
        description="Every dataset entity ID referenced anywhere in these five text fields")


class NarrativeRejected(ValueError):
    """A draft failed a deterministic provenance or presentation check."""


def sentence_count(text: str) -> int:
    return len([part for part in re.split(r"(?<=[.!?])\s+", text.strip()) if part.strip()])


def validate_draft(draft: NarrativeDraft, brief: dict, fallback: dict[str, str],
                   allowed_ids: set[str]) -> dict[str, str]:
    """Check citations, unchanged decision constraints and disclosed simulations."""
    words = {field: getattr(draft, field) for field in TEXT_FIELDS}
    text = "\n".join(words.values())
    citations = set(draft.cited_entity_ids)
    allowed = {str(item) for item in allowed_ids}
    mentioned = set(ENTITY_ID.findall(text))
    if not citations <= allowed or not mentioned <= allowed:
        raise NarrativeRejected("unknown_entity")
    # Also cover exact device/email IDs which are not recognizable by ID shape.
    for entity in allowed:
        if entity and re.search(r"(?<![A-Za-z0-9])" + re.escape(entity) + r"(?![A-Za-z0-9-])", text):
            mentioned.add(entity)
    if not mentioned <= citations:
        raise NarrativeRejected("uncited_entity")
    without_profiles = text
    for profile in sorted((entity for entity in allowed if "|" in entity), key=len, reverse=True):
        without_profiles = without_profiles.replace(profile, "")
    if "|" in without_profiles:
        raise NarrativeRejected("unrecognized_device_profile")
    for match in COMPLETED_ACTION.finditer(text):
        # Explicit negative statements such as "no cards were blocked" are safe.
        before = text[max(0, match.start() - 12):match.start()].lower()
        if not re.search(r"\b(?:no|not|never)\s*$", before):
            raise NarrativeRejected("unexecuted_action_claim")
    if not 2 <= sentence_count(words["summary"]) <= 6:
        raise NarrativeRejected("summary_length")
    report_required = any(
        row.get("action") == "FILE_REPORT" for row in brief.get("final_actions", []))
    if report_required:
        if not 6 <= sentence_count(words["sar_narrative"]) <= 12:
            raise NarrativeRejected("sar_length")
    elif words["sar_narrative"]:
        raise NarrativeRejected("unexpected_sar")
    if brief.get("pattern") == "undocumented":
        if not 2 <= sentence_count(words["pattern_description"]) <= 3:
            raise NarrativeRejected("pattern_description_length")
    elif words["pattern_description"]:
        raise NarrativeRejected("unexpected_pattern_description")
    if fallback.get("what_changed") == "nothing" and words["what_changed"] != "nothing":
        raise NarrativeRejected("invented_action_change")
    if words["what_changed"] != "nothing" and not 1 <= sentence_count(words["what_changed"]) <= 2:
        raise NarrativeRejected("change_description_length")
    if not words["stop_reason"].strip():
        raise NarrativeRejected("missing_stop_reason")
    if any(request.get("source", "simulated") == "simulated" for request in brief.get("requests", [])):
        required = ["summary", "what_changed"] + \
            (["sar_narrative"] if report_required else [])
        if any("simulat" not in words[field].lower() for field in required if words[field] != "nothing"):
            raise NarrativeRejected("missing_simulation_disclosure")
    if any(request.get("source") == "human" for request in brief.get("requests", [])):
        required = ["summary", "what_changed"] + (["sar_narrative"] if report_required else [])
        if any("human" not in words[field].lower() for field in required if words[field] != "nothing"):
            raise NarrativeRejected("missing_human_evidence_disclosure")
    # A prose editor cannot change an explicit verdict or introduce new actions.
    verdict = str(brief.get("verdict", ""))
    stated_verdicts = re.findall(
        r"\b(?:verdict\s*(?:is|was|:)\s*|assessed as\s+|classified as\s+)(fraud|legitimate|uncertain)\b", text, re.I)
    if verdict and any(value.lower() != verdict for value in stated_verdicts):
        raise NarrativeRejected("changed_verdict")
    probabilities = re.findall(
        r"\b(?:fraud\s+)?probability\s+(?:is\s+|of\s+|was\s+)?(0(?:\.\d+)?|1(?:\.0+)?)\b", text, re.I)
    if "probability" in brief and any(abs(float(value) - float(brief["probability"])) > .00051 for value in probabilities):
        raise NarrativeRejected("changed_probability")
    allowed_actions = {row["action"] for key in ("initial_actions", "final_actions")
                       for row in brief.get(key, [])}
    action_ids = set(re.findall(r"\b(?:ALLOW_TRANSACTION|DECLINE_TRANSACTION|MONITOR_CARD|MONITOR_CONNECTED_CARDS|WARN_CUSTOMER|VERIFY_WITH_CUSTOMER|STEP_UP_AUTH|BLOCK_CARD|BLOCK_ALL_CARDS|GENERATE_REPORT|CREATE_CASE|FILE_REPORT|ESCALATE_TO_ANALYST|CLOSE_NO_FRAUD)\b", text))
    if not action_ids <= allowed_actions:
        raise NarrativeRejected("invented_action")
    return words


SYSTEM_PROMPT = """You are the explanation layer of a fraud investigation agent at a bank. A deterministic policy
engine has already decided the verdict, fraud pattern, actions and approval routes. You never change
those decisions. Your job is to explain them clearly, accurately and defensibly for a fraud analyst
and a regulator.

You will receive a JSON evidence brief containing: the case trigger, verdict, probability, pattern,
evidence items (each with source, reference ID and entity IDs), retrieved policy clauses, similar
precedent cases with outcomes, the initial and final next best actions with the rules that justify
them, evidence requests with assumed responses, and whether a SAR is required.

Rules:
1. Use only facts in the brief. Never invent transaction IDs, card IDs, amounts, dates or case IDs.
   Cite IDs exactly as given.
2. Cite the policy rule (for example "R1", "section 3a") behind every action you describe.
3. State uncertainty honestly: say what is known, what is not known, and why more evidence was or
   was not requested.
4. For what_changed: explain in 1-2 sentences how the new evidence moved the assessment and why the
   recommendation changed or stayed the same.
5. For stop_reason: explain which stopping condition was met (confidence threshold plus
   independent evidence, or the policy action that ends the investigation).
6. For SAR narratives: write 6-12 factual sentences covering who, what, when, where, how and why the
   activity is suspicious, in neutral regulatory language, with no speculation beyond the evidence.
   If final_actions does not contain FILE_REPORT, return an empty string for the narrative.
7. If the verdict is legitimate, explain which evidence cleared it. Don't hedge toward fraud.
8. Plain professional English. No markdown, no emojis.
9. Keep summary to 2-6 sentences. Use an empty pattern_description unless the pattern is
   undocumented; then use 2-3 sentences. If the fallback what_changed is "nothing", keep it exactly.
10. Describe bank actions as recommendations. When requests contains simulated replies,
    explicitly call them simulated in summary, what_changed (unless "nothing"), and any SAR.
    When requests contains human evidence, explicitly call it human-recorded in those fields.
    An analyst records evidence in a local demo role; do not imply authenticated customer contact.
11. List every entity ID mentioned in the text in cited_entity_ids, using only allowed_entity_ids.

Return only JSON matching the provided schema:
{"summary": str, "pattern_description": str, "what_changed": str,
 "stop_reason": str, "sar_narrative": str, "cited_entity_ids": list[str]}
"""


def _value(obj: Any, key: str, default: Any = None) -> Any:
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def record_usage(response: Any, metrics: dict) -> None:
    usage = _value(response, "usage_metadata")
    if usage is None:
        usage = _value(_value(response, "response", {}), "usage_metadata")
    if usage is None:
        return
    inputs = int(_value(usage, "prompt_token_count", 0) or 0)
    candidates = int(_value(usage, "candidates_token_count", 0) or 0)
    thoughts = int(_value(usage, "thoughts_token_count", 0) or 0)
    metrics["input_tokens"] += inputs
    metrics["output_tokens"] += candidates + thoughts
    metrics["thought_tokens"] += thoughts
    metrics["reported_total_tokens"] += int(_value(usage, "total_token_count",
                                            inputs + candidates + thoughts) or inputs + candidates + thoughts)


logger = logging.getLogger(__name__)


class Narrator:
    def __init__(self, *, enabled: bool | None = None, model: str | None = None,
                 timeout_s: float = 30, retry_min_s: float = 1, retry_max_s: float = 4):
        self.enabled = configured() and enabled is not False
        self.models = model_chain("gen", model) if self.enabled else []
        self.quota = QuotaState("gen")
        self.model = self.models[0] if self.models else ""
        self.timeout_s = timeout_s
        self.retry_min_s = retry_min_s
        self.retry_max_s = retry_max_s

    def _generate(self, contents: str, model: str):
        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            thinking_config=(types.ThinkingConfig(thinking_budget=1024) if "gemini-2.5" in model else
                             types.ThinkingConfig(thinking_level=types.ThinkingLevel.HIGH) if re.match(r"gemini-[3-9]", model) else None),
            response_mime_type="application/json",
            # Gemini's response_schema dialect rejects additionalProperties.
            # Keep extra=forbid in local validation, omit it only on the wire.
            response_schema={k: v for k, v in NarrativeDraft.model_json_schema().items()
                             if k != "additionalProperties"},
        )
        if model.startswith("gemma-"):
            # Gemma API models do not support system instructions or JSON mode.
            config = types.GenerateContentConfig()
            contents = SYSTEM_PROMPT + "\n" + contents
        return get_client("gen").models.generate_content(model=model, contents=contents, config=config)

    async def __call__(self, brief: dict, fallback: dict[str, str],
                       allowed_ids: set[str]) -> tuple[dict[str, str], dict]:
        started = time.perf_counter()
        metrics = {"input_tokens": 0, "output_tokens": 0, "thought_tokens": 0,
                   "reported_total_tokens": 0, "llm_latency_s": 0.0, "llm_attempts": 0,
                   "narrative_source": "template", "llm_model": self.model,
                   "llm_model_used": "template", "llm_calls": [],
                   "llm_fallback_reason": "disabled_or_unconfigured"}
        words = dict(fallback)
        if not self.enabled:
            metrics["llm_latency_s"] = round(time.perf_counter() - started, 4)
            return words, metrics
        metrics["llm_fallback_reason"] = "no_discovered_models" if not self.models else "all_models_cooling_down"
        try:
            contents = json.dumps({"evidence_brief": brief, "fallback_templates": fallback,
                                   "allowed_entity_ids": sorted(allowed_ids)}, allow_nan=False)
            for model in self.models:
                if self.quota.remaining(model) > 0:
                    continue
                # Retries belong to this call; subsequent calls honor the breaker.
                for attempt in range(3):
                    metrics["llm_attempts"] += 1
                    call = {"model": model, "attempt": attempt + 1, "input_tokens": 0,
                            "output_tokens": 0, "thought_tokens": 0, "reported_total_tokens": 0}
                    delay = None
                    try:
                        try:
                            response = await asyncio.to_thread(self._generate, contents, model)
                        except Exception as exc:
                            record_usage(exc, call)
                            raise
                        record_usage(response, call)
                        parsed = _value(response, "parsed")
                        text = _value(response, "text", "") or ""
                        if isinstance(text, str):
                            text = re.sub(r"^```(?:json)?\s*([\s\S]*?)\s*```$", r"\1", text.strip())
                        draft = (parsed if isinstance(parsed, NarrativeDraft) else
                                 NarrativeDraft.model_validate(parsed) if isinstance(parsed, dict) else
                                 NarrativeDraft.model_validate_json(text))
                        words = validate_draft(draft, brief, fallback, allowed_ids)
                        metrics.update(narrative_source="gemini", llm_model_used=model,
                                       llm_model=model, llm_fallback_reason="")
                        call["outcome"] = "accepted"
                        return words, metrics
                    except Exception as exc:
                        call["outcome"] = type(exc).__name__
                        call["status_code"] = status_code(exc)
                        if isinstance(exc, NarrativeRejected):
                            call["rejection_reason"] = str(exc)
                        metrics["llm_fallback_reason"] = type(exc).__name__
                        if rate_limited(exc):
                            entry = self.quota.record(model, exc)
                            call["quota_kind"] = entry["kind"]
                            call["quota_reset_at"] = entry["reset_at"]
                            # Try another model immediately; future calls honor the cooldown.
                        elif transient(exc) and attempt < 2:
                            delay = (2, 5)[attempt]
                        elif attempt == 0 and not transient(exc) and status_code(exc) is None:
                            # Preserve the existing second chance for rejected prose.
                            delay = 0
                    finally:
                        for key in ("input_tokens", "output_tokens", "thought_tokens", "reported_total_tokens"):
                            metrics[key] += call[key]
                        metrics["llm_calls"].append(call)
                        logger.warning("Narration call %s", json.dumps(call))
                    if delay is None:
                        break
                    await asyncio.sleep(delay)
        except Exception as exc:
            metrics["llm_fallback_reason"] = type(exc).__name__
        finally:
            metrics["llm_latency_s"] = round(time.perf_counter() - started, 4)
        return dict(fallback), metrics
