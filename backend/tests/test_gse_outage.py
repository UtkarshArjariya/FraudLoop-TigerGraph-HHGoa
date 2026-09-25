"""The stream must not repeatedly restart GSE during a continuing outage."""
import pytest
from backend.data import Repository
from backend.services import GraphGateway


@pytest.mark.asyncio
async def test_one_restart_per_outage_and_recovery(tmp_path, monkeypatch):
    monkeypatch.setenv('FRAUDLOOP_LOCAL_ONLY', '0')
    gateway = GraphGateway(Repository(tmp_path))
    calls = []
    state = {'status': 'down'}

    async def command(text):
        calls.append(text)
        return state['status']

    async def no_wait(_):
        pass

    monkeypatch.setattr('backend.services.docker_gadmin', command)
    monkeypatch.setattr('backend.services.asyncio.sleep', no_wait)
    assert await gateway.gse_for_write() is False
    assert await gateway.gse_for_write() is False
    assert calls.count('gadmin restart gse -y') == 1
    assert gateway.write_warning
    state['status'] = 'up'
    assert await gateway.gse_for_write() is True
    assert not gateway.write_warning
    state['status'] = 'down'
    assert await gateway.gse_for_write() is False
    assert calls.count('gadmin restart gse -y') == 2
