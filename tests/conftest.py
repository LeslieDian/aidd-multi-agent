import socket
import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

@pytest.fixture(autouse=True)
def isolate_tests(tmp_path, monkeypatch):
    from agents.failed_set import FailedLigandSet
    monkeypatch.setattr(FailedLigandSet, 'DEFAULT_PATH', tmp_path / 'failed.json')
    def deny(*args, **kwargs):
        raise AssertionError('Network disabled in offline tests')
    monkeypatch.setattr(socket.socket, 'connect', deny)
