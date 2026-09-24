import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from google.genai.errors import ClientError

from agent.quota import QuotaState, next_pacific_midnight
from agent.embeddings import GeminiEmbedder, embed_vectors


def quota_error(quota_id, delay=None):
    details = [{'@type': 'type.googleapis.com/google.rpc.QuotaFailure', 'violations': [{'quotaId': quota_id}]}]
    if delay is not None:
        details.append({'@type': 'type.googleapis.com/google.rpc.RetryInfo', 'retryDelay': delay})
    return ClientError(429, {'error': {'status': 'RESOURCE_EXHAUSTED', 'details': details}})


@pytest.mark.parametrize('date,hours', [('2026-03-08', 23), ('2026-11-01', 25)])
def test_daily_expiry_uses_pacific_midnight_through_dst(date, hours):
    start = datetime.fromisoformat(date).replace(tzinfo=ZoneInfo('America/Los_Angeles')).timestamp()
    assert next_pacific_midnight(start) - start == hours * 3600


def test_daily_takes_precedence_over_minute_and_survives_restart(tmp_path):
    now = 1780000000
    path = tmp_path / 'quota.json'
    state = QuotaState(path=path, scope='project-a', clock=lambda: now)
    entry = state.record('flash', quota_error('GenerateRequestsPerDay', '2s'))
    assert entry['until'] == next_pacific_midnight(now)
    state.record('flash', quota_error('GenerateRequestsPerMinute', '30s'))
    restarted = QuotaState(path=path, scope='project-a', clock=lambda: now)
    assert restarted.remaining('flash') == entry['until'] - now
    assert QuotaState(path=path, scope='project-b', clock=lambda: now).remaining('flash') == 0


def test_minute_default_and_retry_info(tmp_path):
    state = QuotaState(path=tmp_path / 'quota.json', scope='a', clock=lambda: 100)
    assert state.record('one', quota_error('RequestsPerMinute'))['until'] == 130
    assert state.record('two', quota_error('RequestsPerMinute', '7.25s'))['until'] == 107.25


def test_embedding_daily_stops_without_retry(monkeypatch):
    async def to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)
    monkeypatch.setattr(asyncio, 'to_thread', to_thread)
    client = MagicMock()
    client.models.embed_content.side_effect = quota_error('EmbedContentRequestsPerDay')
    provider = GeminiEmbedder(client=client)
    with pytest.raises(ClientError):
        asyncio.run(embed_vectors(provider, ['synthetic'], wait_for_quota=True))
    assert client.models.embed_content.call_count == 1 and provider.quota.remaining(provider.model_id) > 0


def test_embedding_minute_waits_then_retries_same_model(monkeypatch):
    now = [100.0]
    sleeps = []
    async def sleep(delay):
        sleeps.append(delay)
        now[0] += delay
    async def to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)
    monkeypatch.setattr(asyncio, 'sleep', sleep)
    monkeypatch.setattr(asyncio, 'to_thread', to_thread)
    client = MagicMock()
    client.models.embed_content.side_effect = [quota_error('RequestsPerMinute', '7s'),
        SimpleNamespace(embeddings=[SimpleNamespace(values=[1.] + [0.] * 767)])]
    provider = GeminiEmbedder(client=client, quota=QuotaState('embed', clock=lambda: now[0]))
    assert len(asyncio.run(embed_vectors(provider, ['synthetic'], wait_for_quota=True))) == 1
    assert sleeps == [7] and client.models.embed_content.call_count == 2
