import socket
import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_REAL_SOCKET_CONNECT = socket.socket.connect

@pytest.fixture(autouse=True)
def isolate_tests(tmp_path, monkeypatch):
    from agents.failed_set import FailedLigandSet
    monkeypatch.setattr(FailedLigandSet, 'DEFAULT_PATH', tmp_path / 'failed.json')
    def deny(sock, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) and address else ""
        if host in {"127.0.0.1", "::1", "localhost"}:
            return _REAL_SOCKET_CONNECT(sock, address, *args, **kwargs)
        raise AssertionError('Network disabled in offline tests')
    monkeypatch.setattr(socket.socket, 'connect', deny)
