import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from google.genai import types

import agent.llm as llm


ALLOWED = {"HHG-001", "C12345", "C12345-K1", "3456789"}
WORDS = {"summary": "C12345-K1 was assessed as uncertain. The evidence does not settle authorization.",
         "pattern_description": "", "sar_narrative": "", "what_changed": "nothing",
         "stop_reason": "R8: an analyst must resolve the remaining authorization uncertainty."}
BRIEF = {"verdict": "uncertain", "pattern": "none", "requests": [], "initial_actions": [], "final_actions": []}


def response(words=None, citations=None):
    return SimpleNamespace(parsed=llm.NarrativeDraft(**(words or WORDS),
                           cited_entity_ids=citations if citations is not None else ["C12345-K1"]),
                           usage_metadata=SimpleNamespace(prompt_token_count=10, candidates_token_count=20,
                                                          thoughts_token_count=3, total_token_count=33))


@pytest.fixture
def sdk(monkeypatch):
    from agent import model_clients
    monkeypatch.setattr(llm, "configured", lambda: True)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-never-sent-to-a-service")
    monkeypatch.setattr(llm, "model_chain", lambda *args: ["gemini-3.0-flash"])
    async def to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)
    monkeypatch.setattr(llm.asyncio, "to_thread", to_thread)
    client = MagicMock()
    client.__enter__.return_value = client
    client.models.generate_content.return_value = response()
    factory = MagicMock(return_value=client)
    monkeypatch.setattr(model_clients.genai, "Client", factory)
    return factory, client


def narrate(**kwargs):
    return llm.Narrator(retry_min_s=0, retry_max_s=0, **kwargs)


def test_success_uses_high_thinking_structured_json_and_counts_thoughts(sdk):
    factory, client = sdk
    words, metrics = asyncio.run(narrate()(BRIEF, WORDS, ALLOWED))
    assert words == WORDS and metrics["narrative_source"] == "gemini"
    assert metrics["input_tokens"] == 10 and metrics["output_tokens"] == 23
    assert metrics["thought_tokens"] == 3 and metrics["reported_total_tokens"] == 33
    assert metrics["llm_attempts"] == 1 and metrics["llm_latency_s"] >= 0
    kwargs = client.models.generate_content.call_args.kwargs
    assert kwargs["model"] == "gemini-3.0-flash"
    assert kwargs["config"].thinking_config.thinking_level == types.ThinkingLevel.HIGH
    assert "summary" in kwargs["config"].response_schema["properties"]
    assert "additionalProperties" not in kwargs["config"].response_schema
    assert kwargs["config"].response_mime_type == "application/json"
    supplied = kwargs["config"].model_dump(exclude_unset=True)
    assert not {"temperature", "top_p", "top_k"} & supplied.keys()
    assert factory.call_args.kwargs["http_options"].retry_options.attempts == 1
    assert "test-key" not in json.dumps(metrics)


def test_two_sdk_failures_fall_back_without_exposing_exception_text(sdk):
    _, client = sdk
    client.models.generate_content.side_effect = RuntimeError("secret-test-key in private request")
    words, metrics = asyncio.run(narrate()(BRIEF, WORDS, ALLOWED))
    assert words == WORDS and metrics["narrative_source"] == "template"
    assert client.models.generate_content.call_count == 2 and metrics["llm_attempts"] == 2
    assert metrics["input_tokens"] == metrics["output_tokens"] == 0
    assert "secret" not in json.dumps(metrics)


def test_unknown_or_uncited_ids_reject_drafts_and_count_rejected_usage(sdk):
    _, client = sdk
    hallucination = {**WORDS, "summary": "C99999-K1 was assessed as uncertain. The analyst will inspect 9999999."}
    client.models.generate_content.return_value = response(hallucination, ["C99999-K1", "9999999"])
    words, metrics = asyncio.run(narrate()(BRIEF, WORDS, ALLOWED))
    assert words == WORDS and metrics["narrative_source"] == "template"
    assert metrics["llm_attempts"] == 2
    assert metrics["input_tokens"] == 20 and metrics["output_tokens"] == 46
    client.models.generate_content.return_value = response(citations=[])
    _, metrics = asyncio.run(narrate()(BRIEF, WORDS, ALLOWED))
    assert metrics["narrative_source"] == "template"


@pytest.mark.parametrize("summary", [
    "C12345-K1 was assessed as uncertain. The card was blocked.",
    "C12345-K1 was assessed as uncertain. Card C12345-K1 was blocked.",
    "C12345-K1 was assessed as uncertain. The report has been filed.",
    "C12345-K1 was assessed as uncertain. The customer was contacted.",
    "C12345-K1 was assessed as fraud. The decision is final.",
    "C12345-K1 was assessed as uncertain. Device Fake Phone|Fake OS|Browser|Screen was observed.",
])
def test_execution_claims_decision_changes_and_invented_profiles_are_rejected(sdk, summary):
    _, client = sdk
    client.models.generate_content.return_value = response({**WORDS, "summary": summary})
    words, metrics = asyncio.run(narrate()(BRIEF, WORDS, ALLOWED))
    assert words == WORDS and metrics["narrative_source"] == "template"


def test_simulated_replies_must_remain_explicit(sdk):
    _, client = sdk
    brief = {**BRIEF, "requests": [{"type": "customer_validation", "assumed_response": "SIMULATED denial"}]}
    fallback = {**WORDS, "what_changed": "A simulated reply retained uncertainty."}
    client.models.generate_content.return_value = response({**WORDS, "what_changed": "The customer confirmed authorization."})
    words, metrics = asyncio.run(narrate()(brief, fallback, ALLOWED))
    assert words == fallback and metrics["narrative_source"] == "template"


def test_unconfigured_service_never_constructs_a_client(sdk, monkeypatch):
    factory, _ = sdk
    monkeypatch.setattr(llm, "configured", lambda: False)
    words, metrics = asyncio.run(narrate(enabled=True)(BRIEF, WORDS, ALLOWED))
    assert words == WORDS and metrics["llm_attempts"] == 0
    factory.assert_not_called()


def test_valid_retry_counts_both_attempts_and_failed_response_usage(sdk):
    _, client = sdk
    error = RuntimeError("private service context")
    error.usage_metadata = {"prompt_token_count": 4, "candidates_token_count": 0, "thoughts_token_count": 2}
    client.models.generate_content.side_effect = [error, response()]
    words, metrics = asyncio.run(narrate()(BRIEF, WORDS, ALLOWED))
    assert words == WORDS and metrics["narrative_source"] == "gemini"
    assert metrics["llm_attempts"] == 2 and metrics["input_tokens"] == 14
    assert metrics["output_tokens"] == 25 and metrics["thought_tokens"] == 5


def test_sar_and_summary_sentence_requirements_are_enforced(sdk):
    _, client = sdk
    brief = {**BRIEF, "final_actions": [{"action": "FILE_REPORT"}]}
    too_short = {**WORDS, "sar_narrative": "This report is recommended for approval."}
    client.models.generate_content.return_value = response(too_short)
    _, metrics = asyncio.run(narrate()(brief, WORDS, ALLOWED))
    assert metrics["narrative_source"] == "template"
    client.models.generate_content.return_value = response({**WORDS, "summary": "Only one sentence."})
    _, metrics = asyncio.run(narrate()(BRIEF, WORDS, ALLOWED))
    assert metrics["narrative_source"] == "template"


@pytest.mark.parametrize('code,status', [(503, 'UNAVAILABLE')])
def test_transient_chain_retries_twice_then_uses_next_model(sdk, monkeypatch, code, status):
    from google.genai.errors import ClientError
    _, client = sdk
    monkeypatch.setattr(llm, 'model_chain', lambda *args: ['first', 'second'])
    sleeps = []
    async def sleep(delay):
        sleeps.append(delay)
    monkeypatch.setattr(llm.asyncio, 'sleep', sleep)
    exc = ClientError(code, {'error': {'status': status, 'message': 'busy'}})
    client.models.generate_content.side_effect = [exc, exc, exc, response()]
    words, metrics = asyncio.run(narrate()(BRIEF, WORDS, ALLOWED))
    assert words == WORDS and metrics['llm_model_used'] == 'second'
    assert [c.kwargs['model'] for c in client.models.generate_content.call_args_list] == ['first'] * 3 + ['second']
    assert sleeps == [2, 5] and metrics['llm_attempts'] == 4
    assert metrics['llm_calls'][-1]['reported_total_tokens'] == 33


@pytest.mark.parametrize("quota_id,seconds", [("GenerateRequestsPerDayPerProjectPerModel-FreeTier", 86400),
                                             ("GenerateRequestsPerMinutePerProjectPerModel-FreeTier", 30)])
def test_quota_persists_across_narrators_and_skips_model(sdk, monkeypatch, quota_id, seconds):
    from google.genai.errors import ClientError
    from agent.quota import QuotaState
    _, client = sdk
    monkeypatch.setattr(llm, 'model_chain', lambda *args: ['first', 'second'])
    now = [1704067200.0]
    quota = QuotaState('gen', clock=lambda: now[0])
    monkeypatch.setattr(llm, 'QuotaState', lambda *args: QuotaState('gen', clock=lambda: now[0]))
    exc = ClientError(429, {'error': {'status': 'RESOURCE_EXHAUSTED', 'details': [
        {'@type': 'type.googleapis.com/google.rpc.QuotaFailure', 'violations': [{'quotaId': quota_id}]},
        {'@type': 'type.googleapis.com/google.rpc.RetryInfo', 'retryDelay': '30s'}]}})
    client.models.generate_content.side_effect = [exc, response(), response(), response()]
    _, metrics = asyncio.run(narrate()(BRIEF, WORDS, ALLOWED))
    assert metrics['llm_attempts'] == 2 and metrics['llm_model_used'] == 'second'
    assert metrics['llm_calls'][0]['quota_kind'] == ('daily' if 'PerDay' in quota_id else 'minute')
    _, metrics = asyncio.run(narrate()(BRIEF, WORDS, ALLOWED))
    assert metrics['llm_attempts'] == 1 and metrics['llm_model_used'] == 'second'
    now[0] += quota.remaining('first') + 1
    _, metrics = asyncio.run(narrate()(BRIEF, WORDS, ALLOWED))
    assert metrics['llm_model_used'] == 'first'


def test_all_models_fail_returns_template_and_counts_all_calls(sdk, monkeypatch):
    _, client = sdk
    monkeypatch.setattr(llm, 'model_chain', lambda *args: ['first', 'second'])
    async def sleep(delay):
        pass
    monkeypatch.setattr(llm.asyncio, 'sleep', sleep)
    client.models.generate_content.side_effect = RuntimeError('503 UNAVAILABLE')
    words, metrics = asyncio.run(narrate()(BRIEF, WORDS, ALLOWED))
    assert words == WORDS and metrics['llm_model_used'] == 'template'
    assert metrics['llm_attempts'] == 6


def test_empty_checked_chain_never_calls_sdk(sdk, monkeypatch):
    factory, _ = sdk
    monkeypatch.setattr(llm, 'model_chain', lambda *args: [])
    _, metrics = asyncio.run(narrate()(BRIEF, WORDS, ALLOWED))
    assert metrics['llm_model_used'] == 'template'
    assert metrics['llm_fallback_reason'] == 'no_discovered_models'
    factory.assert_not_called()


def test_gemma_uses_plain_json_prompt_and_guarded_fence_parsing(sdk, monkeypatch):
    _, client = sdk
    monkeypatch.setattr(llm, 'model_chain', lambda *args: ['gemma-3-27b-it'])
    client.models.generate_content.return_value = SimpleNamespace(parsed=None,
        text='```json\n' + json.dumps({**WORDS, 'cited_entity_ids': ['C12345-K1']}) + '\n```')
    words, metrics = asyncio.run(narrate()(BRIEF, WORDS, ALLOWED))
    assert words == WORDS and metrics['llm_model_used'] == 'gemma-3-27b-it'
    config = client.models.generate_content.call_args.kwargs['config']
    assert config.system_instruction is None and config.thinking_config is None and config.response_schema is None
    assert llm.SYSTEM_PROMPT in client.models.generate_content.call_args.kwargs['contents']
