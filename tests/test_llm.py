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
    """Mock client always works, no key needed."""
    cfg = load_config()
    client = get_client("deepseek", cfg, mock=True)
    assert isinstance(client, MockLLMClient)
    out = client.chat("sys", "user", json_mode=True)
    assert "smiles_list" in out, f"unexpected: {out[:200]}"
    print(f"  OK  mock chat returned {len(out)} chars")


def test_real_client_construction():
    """If keys are in env, real client is returned. Otherwise mock."""
    cfg = load_config()
    for name in ["deepseek", "MiniMax"]:
        client = get_client(name, cfg)
        kind = type(client).__name__
        print(f"  OK  {name} -> {kind}")
        assert isinstance(client, (LLMClient, MockLLMClient))


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