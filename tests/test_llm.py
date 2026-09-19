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


def test_chat_json_accepts_first_complete_object_but_keeps_schema_validation_separate():
    client = object.__new__(LLMClient)
    client.chat = lambda *args, **kwargs: 'prefix {"tool":"evaluate","arguments":{},"reason":"ok"} trailing text'
    parsed = client.chat_json("system", "user")
    assert parsed["tool"] == "evaluate"
    assert parsed["reason"] == "ok"


def test_proxy_policy_is_explicit_and_tls_remains_verified():
    import copy
    import os
    from unittest.mock import patch
    import agents.llm as llm_module
    cfg = copy.deepcopy(load_config())
    name = next(iter(cfg["llm"]["providers"]))
    cfg["llm"]["providers"][name].update(timeout=17, max_retries=4)
    captured = {}
    sentinel_http = object()

    def fake_http_client(**kwargs):
        captured["http"] = kwargs
        return sentinel_http

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured["openai"] = kwargs

    with patch.object(llm_module.httpx, "Client", side_effect=fake_http_client), \
         patch.object(llm_module, "OpenAI", FakeOpenAI), \
         patch.dict(os.environ, {cfg['llm']['providers'][name]['api_key_env']: 'test-placeholder'}):
        client = get_client(name, cfg)
    assert captured["http"] == {"trust_env": False, "verify": True}
    assert captured["openai"]["http_client"] is sentinel_http
    assert captured["openai"]["base_url"] == cfg["llm"]["providers"][name]["base_url"]
    assert captured["openai"]["timeout"] == 17
    assert captured["openai"]["max_retries"] == 4
    assert captured["openai"]["api_key"] == "test-placeholder"
    assert client.model == cfg["llm"]["providers"][name]["model"]


if __name__ == "__main__":
    print("== LLM client tests ==")
    test_mock_client()
    test_real_client_construction()
    test_get_client_unknown_provider()
    print("\n[OK] All LLM tests passed (no real API calls).")
