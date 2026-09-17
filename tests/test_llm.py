"""tests/test_llm.py - Verify LLM client construction (no real API calls)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import yaml

from agents import get_client, MockLLMClient, LLMClient


def load_config() -> dict:
    with open("config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_mock_client():
    """Mock client always works, no key needed.

    Uses whatever provider is first in config.yaml. As of 2026-09-17 the
    project runs in single-provider mode (MiniMax); deepseek remains
    available in the harness but its provider block is commented out.
    """
    cfg = load_config()
    provider_name = next(iter(cfg["llm"]["providers"]))
    client = get_client(provider_name, cfg, mock=True)
    assert isinstance(client, MockLLMClient)
    out = client.chat("sys", "user", json_mode=True)
    assert "smiles_list" in out, f"unexpected: {out[:200]}"
    print(f"  OK  mock chat returned {len(out)} chars (provider={provider_name})")


def test_real_client_construction():
    """Explicit mock only; missing credentials must fail."""
    import os
    from unittest.mock import patch
    cfg = load_config()
    for name in cfg["llm"]["providers"].keys():
        with patch.dict(os.environ, {}, clear=True):
            try:
                get_client(name, cfg)
            except EnvironmentError:
                pass
            else:
                raise AssertionError("Missing key silently accepted")
        with patch.dict(os.environ, {cfg['llm']['providers'][name]['api_key_env']: 'test-placeholder'}):
            assert isinstance(get_client(name, cfg), LLMClient)


def test_get_client_unknown_provider():
    cfg = load_config()
    try:
        get_client("nope", cfg)
        raise AssertionError("expected KeyError")
    except KeyError as e:
        print(f"  OK  unknown provider raises KeyError: {e}")


if __name__ == "__main__":
    print("== LLM client tests ==")
    test_mock_client()
    test_real_client_construction()
    test_get_client_unknown_provider()
    print("\n[OK] All LLM tests passed (no real API calls).")