"""Model tests must never touch operator keys, checkpoints, or paid services."""
import pytest


@pytest.fixture(autouse=True)
def isolated_model_state(tmp_path, monkeypatch):
    import agent.embeddings as emb
    import agent.model_clients as clients
    monkeypatch.setenv('MODEL_REGISTRY_PATH', str(tmp_path / 'registry.json'))
    monkeypatch.setenv('QUOTA_STATE_PATH', str(tmp_path / 'quota.json'))
    monkeypatch.setenv('GEMINI_API_KEY', '')
    monkeypatch.setenv('GEMINI_EMBEDDING', '')
    monkeypatch.setenv('GEMINI_MODEL_CHAIN', '')
    monkeypatch.setattr(clients, 'load_dotenv', lambda: None)
    monkeypatch.setattr(emb, 'INDEX_PATH', tmp_path / 'indexes.json')
    monkeypatch.setattr(emb, 'LEGACY_INDEX_PATH', tmp_path / 'legacy.json')
    monkeypatch.setattr(emb, 'LEGACY_CHECKPOINT', tmp_path / 'legacy.txt')
    monkeypatch.setattr(emb, 'CHECKPOINT_DIR', tmp_path / 'checkpoints')
    monkeypatch.setattr(emb, 'CACHE_DIR', tmp_path / 'embeddings')
    clients._client.cache_clear()
    clients._warn_missing_embedding_key.cache_clear()
    emb._PROVIDERS.clear()
    yield
    clients._client.cache_clear()
    emb._PROVIDERS.clear()
