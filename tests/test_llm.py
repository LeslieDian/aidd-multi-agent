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
    # Contract change (2026-09-19): the Harness owns retries, so a provider
    # block can no longer re-enable hidden SDK retries. Previously a config
    # value of 4 was passed through, which let the SDK retry behind the
    # Harness accounting and hide transport failures from the audit trail.
    assert captured["openai"]["max_retries"] == 0
    assert client.model == cfg["llm"]["providers"][name]["model"]


def test_client_close_is_idempotent_and_closes_both_handles():
    import os
    from unittest.mock import patch
    import agents.llm as llm_module
    cfg = load_config()
    name = next(iter(cfg["llm"]["providers"]))
    closed = []
    http_client = type("Http", (), {"close": lambda self: closed.append("http")})()
    sdk_client = type("Sdk", (), {"close": lambda self: closed.append("sdk")})()
    with patch.object(llm_module.httpx, "Client", return_value=http_client), \
         patch.object(llm_module, "OpenAI", return_value=sdk_client), \
         patch.dict(os.environ, {cfg['llm']['providers'][name]['api_key_env']: 'test-placeholder'}):
        client = get_client(name, cfg)
    assert client.closed is False
    client.close()
    client.close()  # must be idempotent, must not raise
    client.close()
    assert client.closed is True
    assert closed == ["sdk", "http"]
    assert client.model == cfg["llm"]["providers"][name]["model"]


def test_http_client_is_created_once_per_provider_and_reused():
    """One policy/task scope must open exactly one HTTP client per provider."""
    import os
    from unittest.mock import patch
    import agents.llm as llm_module
    from agents.harness.reliability import ClientScope
    cfg = load_config()
    name = next(iter(cfg["llm"]["providers"]))
    created = []
    def fake_http_client(**kwargs):
        created.append(kwargs)
        return object()
    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
        def close(self):
            pass
    scope = ClientScope()
    with patch.object(llm_module.httpx, "Client", side_effect=fake_http_client), \
         patch.object(llm_module, "OpenAI", FakeOpenAI), \
         patch.dict(os.environ, {cfg['llm']['providers'][name]['api_key_env']: 'test-placeholder'}):
        first = llm_module.get_client(name, {**cfg, "_client_scope": scope})
        second = llm_module.get_client(name, {**cfg, "_client_scope": scope})
        third = llm_module.get_client(name, {**cfg, "_client_scope_token": scope.token})
    assert first is second is third
    assert len(created) == 1
    assert scope.created == 1
    scope.close()
    scope.close()  # idempotent


def test_token_resolves_without_serializing_the_scope():
    """A token survives JSON/deepcopy round-trips; the scope object does not."""
    import copy
    import json
    import os
    from unittest.mock import patch
    import agents.llm as llm_module
    from agents.harness.reliability import ClientScope, scope_for
    cfg = load_config()
    name = next(iter(cfg["llm"]["providers"]))
    scope = ClientScope()
    assert scope_for(scope.token) is scope
    assert scope_for("not-a-token") is None
    config = {**cfg, "_client_scope_token": scope.token}
    round_tripped = json.loads(json.dumps(config))
    assert round_tripped["_client_scope_token"] == scope.token
    assert scope_for(copy.deepcopy(config)["_client_scope_token"]) is scope
    class FakeOpenAI:
        def __init__(self, **kwargs):
            pass
        def close(self):
            pass
    with patch.object(llm_module.httpx, "Client", return_value=object()), \
         patch.object(llm_module, "OpenAI", FakeOpenAI), \
         patch.dict(os.environ, {cfg['llm']['providers'][name]['api_key_env']: 'test-placeholder'}):
        llm_module.get_client(name, round_tripped)
    assert scope.created == 1
    scope.close()
    assert scope_for(scope.token) is None


def test_api_key_is_never_written_to_evidence_or_exception_text():
    """A fake secret must not appear in evidence, logs, errors or serialized state."""
    import json
    import os
    from unittest.mock import patch
    import agents.llm as llm_module
    fake_key = "sk-THIS-IS-A-FAKE-SECRET-abcdef123456"
    cfg = load_config()
    name = next(iter(cfg["llm"]["providers"]))
    records = []
    class Boom(Exception):
        pass
    class FailingCompletions:
        def create(self, **kwargs):
            raise Boom(f"upstream refused Authorization: Bearer {fake_key} for "
                       f"https://api.example.com/v1/chat?key={fake_key}")
    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.chat = type("Chat", (), {"completions": FailingCompletions()})()
        def close(self):
            pass
    with patch.object(llm_module.httpx, "Client", return_value=object()), \
         patch.object(llm_module, "OpenAI", FakeOpenAI), \
         patch.dict(os.environ, {cfg['llm']['providers'][name]['api_key_env']: fake_key}):
        client = llm_module.get_client(name, cfg, evidence=records.append)
        assert client.api_key == fake_key
        try:
            client.chat("system", "user")
        except Boom as exc:
            error_text = f"{type(exc).__name__}: {exc}"
            redacted_text = exc.sanitized_message
        client.close()
    assert len(records) == 1
    serialized = json.dumps(records, ensure_ascii=False)
    assert fake_key not in serialized
    # The raw provider exception still carries the upstream text (we cannot
    # rewrite a third-party exception), but every layer we control uses the
    # redacted rendering attached to it.
    assert fake_key in error_text
    assert fake_key not in redacted_text
    assert "<redacted>" in redacted_text
    assert records[0]["base_url_host"] == cfg["llm"]["providers"][name]["base_url"].split("//")[1].split("/")[0]
    assert records[0]["exception_category"] == "sdk"
    assert records[0]["response_received"] is False
    assert records[0]["token_usage_status"] == "unavailable"
    assert records[0]["token_usage"] is None
    assert records[0]["outcome"] == "request_failed"
    assert records[0]["thinking"] == cfg["llm"]["providers"][name].get(
        "extra_body", {}).get("thinking", {}).get("type")


def test_schema_parse_failure_is_not_a_network_error():
    import json
    import os
    from unittest.mock import patch
    import agents.llm as llm_module
    from agents.harness.reliability import classify_error
    cfg = load_config()
    name = next(iter(cfg["llm"]["providers"]))
    records = []
    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.chat = type("Chat", (), {"completions": type("C", (), {
                "create": lambda self, **kw: type("R", (), {
                    "usage": None,
                    "choices": [type("Ch", (), {"message": type("M", (), {"content": "not json at all"})()})()],
                })()})()})()
        def close(self):
            pass
    with patch.object(llm_module.httpx, "Client", return_value=object()), \
         patch.object(llm_module, "OpenAI", FakeOpenAI), \
         patch.dict(os.environ, {cfg['llm']['providers'][name]['api_key_env']: 'test-placeholder'}):
        client = llm_module.get_client(name, cfg, evidence=records.append)
        try:
            client.chat_json("system", "user")
        except json.JSONDecodeError as exc:
            parse_error = exc
        client.close()
    assert classify_error(parse_error) is None  # never retried as a network error
    assert [r["outcome"] for r in records] == ["response_received", "schema_error"]
    assert records[1]["schema_valid"] is False
    assert records[1]["exception_category"] == "schema"
    assert records[0]["token_usage_status"] == "unavailable"


if __name__ == "__main__":
    print("== LLM client tests ==")
    test_mock_client()
    test_real_client_construction()
    test_get_client_unknown_provider()
    print("\n[OK] All LLM tests passed (no real API calls).")
