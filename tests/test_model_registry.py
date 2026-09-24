import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from agent import model_clients as clients
from agent.model_registry import classify, discover, model_chain
from agent.service_errors import exception_message


def test_separate_clients_and_missing_embedding_key_warning(monkeypatch, caplog):
    monkeypatch.setenv('GEMINI_API_KEY', 'generation-only-secret')
    monkeypatch.setenv('GEMINI_EMBEDDING', 'embedding-only-secret')
    factory = MagicMock(side_effect=lambda **kwargs: SimpleNamespace(**kwargs))
    monkeypatch.setattr(clients.genai, 'Client', factory)
    gen, embed = clients.get_client('gen'), clients.get_client('embed')
    assert gen is not embed and gen.api_key == 'generation-only-secret' and embed.api_key == 'embedding-only-secret'
    assert clients.get_client('gen') is gen and clients.get_client('embed') is embed
    assert factory.call_count == 2
    monkeypatch.setenv('GEMINI_EMBEDDING', '')
    fallback = clients.get_client('embed')
    assert fallback is not gen and fallback.api_key == gen.api_key
    assert 'GEMINI_EMBEDDING is missing' in caplog.text and 'generation-only-secret' not in caplog.text


def test_both_keys_are_redacted(monkeypatch):
    monkeypatch.setenv('GEMINI_API_KEY', 'gen-secret')
    monkeypatch.setenv('GEMINI_EMBEDDING', 'embed-secret')
    text = exception_message(RuntimeError('gen-secret embed-secret key?key=third-secret'))
    assert 'gen-secret' not in text and 'embed-secret' not in text and 'third-secret' not in text


def model(name, action):
    return SimpleNamespace(name='models/' + name, supported_actions=[action])


def test_classification_and_preference_include_all_text_candidates():
    inventory = classify([model(n, 'generateContent') for n in (
        'gemini-2.5-flash', 'gemini-3-flash-preview', 'gemini-3.5-flash-lite',
        'gemini-2.5-pro', 'gemma-3-27b-it', 'gemini-3-live', 'gemini-3-tts', 'gemini-3-image', 'gemini-native-audio')]
        + [model('gemini-live-only', 'bidiGenerateContent'), model('gemini-embedding-2', 'embedContent')])
    registry = {k: {'models': v} for k, v in inventory.items()}
    assert model_chain('gen', registry=registry)[:3] == ['gemini-3-flash-preview', 'gemini-2.5-flash', 'gemini-3.5-flash-lite']
    assert set(inventory['gen']) == {'gemini-2.5-flash', 'gemini-3-flash-preview', 'gemini-3.5-flash-lite', 'gemini-2.5-pro', 'gemma-3-27b-it'}
    assert inventory['embed'] == ['gemini-embedding-2']
    assert model_chain('gen', 'gemini-2.5-pro,bad,gemini-2.5-flash,gemini-2.5-pro', registry=registry) == ['gemini-2.5-pro', 'gemini-2.5-flash']


def test_each_key_discovers_its_own_models_cache_refresh_and_key_rotation(tmp_path, monkeypatch):
    pairs = {'gen': MagicMock(), 'embed': MagicMock()}
    pairs['gen'].models.list.return_value = [model('gemini-3-flash', 'generateContent')]
    pairs['embed'].models.list.return_value = [model('gemini-embedding-001', 'embedContent')]
    path = tmp_path / 'registry.json'
    first = discover(clients=pairs, path=path, now=100000)
    assert first['gen']['models'] == ['gemini-3-flash'] and first['embed']['models'] == ['gemini-embedding-001']
    discover(clients=pairs, path=path, now=100001)
    assert all(c.models.list.call_count == 1 for c in pairs.values())
    discover(clients=pairs, path=path, now=100002, refresh=True)
    assert all(c.models.list.call_count == 2 for c in pairs.values())
    monkeypatch.setenv('GEMINI_EMBEDDING', 'new-separate-project-key')
    pairs['embed'].models.list.side_effect = RuntimeError('offline')
    failed = discover(clients=pairs, path=path, now=100003)
    assert not failed['embed']['models'] and failed['gen']['models']
    assert 'timestamp' in json.loads(path.read_text())


def test_same_scope_cached_registry_survives_service_outage(tmp_path):
    pairs = {'gen': MagicMock(), 'embed': MagicMock()}
    for kind in pairs:
        pairs[kind].models.list.return_value = [model('gemini-3-flash', 'generateContent')]
    path = tmp_path / 'registry.json'
    discover(clients=pairs, path=path, now=100000)
    for client in pairs.values():
        client.models.list.side_effect = RuntimeError('offline')
    assert discover(clients=pairs, path=path, now=200000)['gen']['models'] == ['gemini-3-flash']
