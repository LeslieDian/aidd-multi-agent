"""tests/test_chainlit_e2e.py - Boots the Chainlit UI and pings HTTP.

Wraps scripts/test_chainlit_e2e.py as a pytest test so it shows up in
the standard ``pytest -q`` suite. Skipped automatically if the
``chainlit`` CLI is not installed (pip install chainlit).

This test verifies:
- ``chainlit_app.py`` loads without import errors.
- The HTTP server binds to the chosen port within ~30 s.
- ``GET /`` returns 200 with a non-trivial HTML body.
- ``GET /health`` returns 2xx/3xx (Chainlit 2.12 may not have this
  endpoint but the port is still reachable).
"""
from __future__ import annotations

import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PYTHON_EXE = sys.executable


def _chainlit_exe() -> str | None:
    exe = shutil.which("chainlit")
    if exe:
        return exe
    scripts = Path(PYTHON_EXE).parent / "Scripts"
    candidate = scripts / ("chainlit.exe" if sys.platform == "win32" else "chainlit")
    if candidate.exists():
        return str(candidate)
    return None


CHAINLIT_AVAILABLE = _chainlit_exe() is not None
SKIP_REASON = "chainlit CLI not installed; run `pip install chainlit`"


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    try:
        return s.getsockname()[1]
    finally:
        s.close()


def _wait_for_port(host: str, port: int, timeout_s: float) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except (ConnectionRefusedError, OSError):
            time.sleep(0.5)
    return False


@pytest.mark.skipif(not CHAINLIT_AVAILABLE, reason=SKIP_REASON)
def test_chainlit_app_boots_and_serves_root():
    """Subprocess-launch chainlit_app.py, wait for the port, GET /."""
    import http.client
    port = _free_port()
    exe = _chainlit_exe()
    cmd = [
        exe, "run", "chainlit_app.py",
        "--host", "127.0.0.1", "--port", str(port),
        "--headless",
    ]
    proc = subprocess.Popen(
        cmd, cwd=ROOT,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        env={**__import__("os").environ,
             "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"},
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    try:
        assert _wait_for_port("127.0.0.1", port, 30.0), (
            "chainlit did not bind the port in 30s; "
            f"stdout: {proc.stdout.read(2000).decode('utf-8', errors='replace') if proc.stdout else ''}"
        )
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request("GET", "/")
            resp = conn.getresponse()
            body = resp.read()
            assert resp.status == 200, f"GET / returned HTTP {resp.status}"
            assert len(body) >= 200, f"GET / body too small ({len(body)} bytes)"
            assert b"<html" in body.lower(), "GET / body is not HTML"
            assert b"chainlit" in body.lower() or b"AIDD" in body, (
                "GET / body has neither 'chainlit' nor 'AIDD' marker"
            )
        finally:
            conn.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.mark.skipif(not CHAINLIT_AVAILABLE, reason=SKIP_REASON)
def test_chainlit_app_serves_health_or_root():
    """Smoke: either /health returns 2xx/3xx, or the UI root is reachable."""
    import http.client
    port = _free_port()
    exe = _chainlit_exe()
    cmd = [
        exe, "run", "chainlit_app.py",
        "--host", "127.0.0.1", "--port", str(port),
        "--headless",
    ]
    proc = subprocess.Popen(
        cmd, cwd=ROOT,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        env={**__import__("os").environ,
             "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"},
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    try:
        assert _wait_for_port("127.0.0.1", port, 30.0)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request("GET", "/health")
            resp = conn.getresponse()
            resp.read()
            assert 200 <= resp.status < 400, (
                f"GET /health returned HTTP {resp.status}; "
                "expected 2xx/3xx"
            )
        except (ConnectionRefusedError, http.client.RemoteDisconnected):
            # Chainlit 2.12 has no /health endpoint; fall back to /
            conn.request("GET", "/")
            resp = conn.getresponse()
            resp.read()
            assert resp.status == 200
        finally:
            conn.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()