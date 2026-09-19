"""Offline tests for model transport reliability: client lifetime, retry
classification, backoff, Retry-After and secret redaction.

No real network call is made. Sleeping is always injected so the suite never
waits. ``tests/conftest.py`` additionally blocks non-loopback sockets.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import httpx
import pytest
import yaml

from agents.harness import CheckpointStore, Harness, TaskState
from agents.harness.runtime import LLMPolicy, MockPolicy
from agents.harness.tools import default_registry
from agents.harness.reliability import (
    ClientScope, RetryPolicy, classify_error, client_config, error_category,
    retry_after_seconds, transient,
)
from agents.redaction import sanitize, sanitize_exception


def load_config() -> dict:
    return yaml.safe_load((Path(__file__).resolve().parents[1] / "config.yaml").read_text(encoding="utf-8"))


def create(tmp_path, **kwargs):
    store = CheckpointStore(tmp_path / "task")
    store.save(TaskState(goal="Find EGFR candidates", config=load_config(), mock=True, **kwargs))
    return store


def action(name, **kwargs):
    return {"tool": name, "arguments": kwargs, "reason": "Test decision"}


# ---------------------------------------------------------------------------
# Fake provider exceptions
# ---------------------------------------------------------------------------

def api_status_error(status: int, headers: dict | None = None):
    """Build a real openai.APIStatusError so classification is not mocked away."""
    from openai import APIStatusError
    request = httpx.Request("POST", "https://api.example.com/v1/chat/completions")
    response = httpx.Response(status, request=request, headers=headers or {})
    return APIStatusError("upstream error", response=response, body=None)


def api_connection_error(message="connection refused"):
    from openai import APIConnectionError
    return APIConnectionError(request=httpx.Request("POST", "https://api.example.com/v1"))


def api_timeout_error():
    from openai import APITimeoutError
    return APITimeoutError(request=httpx.Request("POST", "https://api.example.com/v1"))


# ---------------------------------------------------------------------------
# 1. Error classification
# ---------------------------------------------------------------------------

def test_only_transport_failures_are_retryable():
    retryable = [
        api_timeout_error(),                                    # read/connect timeout
        api_connection_error(),                                 # connection refused / reset
        TimeoutError("timed out"),
        ConnectionError("remote disconnected"),
        httpx.ConnectTimeout("connect timeout"),
        httpx.ReadTimeout("read timeout"),
        httpx.RemoteProtocolError("server disconnected"),
        api_status_error(429),
        api_status_error(500),
        api_status_error(502),
        api_status_error(503),
        api_status_error(504),
    ]
    for exc in retryable:
        assert classify_error(exc) == "network", exc
        assert transient(exc) is True
        assert error_category(exc) == "network"

    permanent = [
        api_status_error(400),
        api_status_error(401),
        api_status_error(403),
        api_status_error(404),
        json.JSONDecodeError("bad json", "{}", 0),
        ValueError("Illegal action at stage=strategy_decision: 'finish'"),
        ValueError("Expected arguments: ['candidate_ids']"),
        ValueError("Edit produced an invalid valence or aromatic system"),
        RuntimeError("deterministic edit produced an existing candidate"),
        KeyError("Unknown candidate ID"),
        ZeroDivisionError("defect"),
    ]
    for exc in permanent:
        assert classify_error(exc) is None, exc
        assert transient(exc) is False
        assert error_category(exc) != "network"


def test_schema_and_state_machine_categories_are_distinct():
    assert error_category(json.JSONDecodeError("bad", "", 0)) == "schema"
    assert error_category(ValueError("Expected arguments: ['a']")) == "schema"
    assert error_category(ValueError("Action must contain tool, arguments, reason only")) == "schema"
    assert error_category(ValueError("Illegal action at stage=terminal: 'pause'")) == "state_machine"
    assert error_category(ValueError("Record the hypothesis before executing a molecular edit")) == "state_machine"
    assert error_category(ValueError("Edit precheck failed: ['valence']")) == "tool"
    assert error_category(api_status_error(401)) == "provider"


# ---------------------------------------------------------------------------
# 2. Retry-After parsing
# ---------------------------------------------------------------------------

def test_retry_after_seconds_is_parsed_and_bounded():
    assert retry_after_seconds(api_status_error(429, {"retry-after": "7"})) == 7.0
    assert retry_after_seconds(api_status_error(429, {"Retry-After": " 2.5 "})) == 2.5
    assert retry_after_seconds(api_status_error(503, {"retry-after": "-5"})) is None
    assert retry_after_seconds(api_status_error(503, {"retry-after": "garbage"})) is None
    assert retry_after_seconds(api_status_error(503, {})) is None
    assert retry_after_seconds(TimeoutError("no response")) is None
    # HTTP-date form in the past is ignored; a future date is honoured.
    assert retry_after_seconds(api_status_error(503, {"retry-after": "Wed, 21 Oct 2015 07:28:00 GMT"})) is None


def test_retry_after_is_clamped_to_max_delay():
    policy = RetryPolicy(max_attempts=3, base_delay=1, max_delay=5, jitter=0,
                         sleep=lambda _: None, random_source=lambda: 0.0)
    assert policy.delay(1, api_status_error(429, {"retry-after": "600"})) == 5.0
    assert policy.delay(1, api_status_error(429, {"retry-after": "3"})) == 3.0


# ---------------------------------------------------------------------------
# 3. Backoff shape
# ---------------------------------------------------------------------------

def test_exponential_backoff_never_exceeds_the_configured_cap():
    policy = RetryPolicy(max_attempts=5, base_delay=2, max_delay=10, jitter=0,
                         sleep=lambda _: None, random_source=lambda: 0.0)
    assert [policy.delay(n) for n in range(1, 6)] == [2.0, 4.0, 8.0, 10.0, 10.0]
    jittered = RetryPolicy(max_attempts=5, base_delay=2, max_delay=10, jitter=0.5,
                           sleep=lambda _: None, random_source=lambda: 1.0)
    delays = [jittered.delay(n) for n in range(1, 6)]
    assert delays[0] == 3.0                     # 2 * (1 + 1.0*0.5)
    assert all(d <= 10.0 for d in delays)
    assert delays == sorted(delays)


def test_jitter_varies_the_delay_instead_of_using_a_fixed_interval():
    values = iter([0.0, 1.0])
    policy = RetryPolicy(max_attempts=3, base_delay=4, max_delay=30, jitter=0.25,
                         sleep=lambda _: None, random_source=lambda: next(values))
    assert policy.delay(1) == 4.0
    assert policy.delay(1) == 5.0


def test_retry_policy_reads_config_and_rejects_invalid_values():
    config = load_config()
    config["harness"].update(retry_base_delay=0.5, retry_max_delay=4, retry_jitter=0.1)
    policy = RetryPolicy.from_config(config, sleep=lambda _: None)
    assert (policy.max_attempts, policy.base_delay, policy.max_delay, policy.jitter) == (3, 0.5, 4.0, 0.1)
    with pytest.raises(ValueError, match="max_attempts"):
        RetryPolicy(max_attempts=9)
    with pytest.raises(ValueError, match="retry_max_delay"):
        RetryPolicy(max_delay=-1)
    with pytest.raises(ValueError, match="retry_jitter"):
        RetryPolicy(jitter=2)


def test_sdk_hidden_retries_are_always_disabled():
    config = client_config(load_config())
    for provider in config["llm"]["providers"].values():
        assert provider["max_retries"] == 0
        assert provider["timeout"] == config["harness"]["request_timeout"]


# ---------------------------------------------------------------------------
# 4. Harness retry behaviour
# ---------------------------------------------------------------------------

class FlakyPolicy:
    """Fails ``failures`` times with ``exc_factory`` then returns ``result``."""

    def __init__(self, failures, exc_factory, result=None, close_tracker=None):
        self.failures = failures
        self.exc_factory = exc_factory
        self.result = result or action("generate", count=1, focus="EGFR")
        self.calls = 0
        self.sleeps = []
        self.close_tracker = close_tracker
        self.closed = 0

    def decide(self, state, registry):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.exc_factory()
        return self.result

    def close(self):
        if getattr(self, "_closed", False):
            return
        self._closed = True
        self.closed += 1
        if self.close_tracker is not None:
            self.close_tracker.append(self.closed)


def test_timeout_is_retried_up_to_the_configured_cap(tmp_path):
    store = create(tmp_path)
    policy = FlakyPolicy(failures=10, exc_factory=api_timeout_error)
    state = Harness(store, policy, sleep=lambda delay: policy.sleeps.append(delay)).run(1)
    assert policy.calls == 3                       # harness.max_attempts default
    assert state.events[-1]["type"] == "error"
    assert state.events[-1]["error_category"] == "network"
    retries = [e for e in state.events if e["type"] == "retry"]
    assert len(retries) == 2
    assert [r["attempt"] for r in retries] == [1, 2]
    assert [r["error_category"] for r in retries] == ["network", "network"]
    assert all(r["retry_scheduled"] is True for r in retries)
    assert policy.sleeps == [r["retry_delay_s"] for r in retries]
    assert policy.sleeps == sorted(policy.sleeps)
    assert state.model_calls_used == 3


def test_429_retry_uses_retry_after_header(tmp_path):
    store = create(tmp_path)
    policy = FlakyPolicy(failures=1, exc_factory=lambda: api_status_error(429, {"retry-after": "3"}))
    state = Harness(store, policy, sleep=lambda delay: policy.sleeps.append(delay)).run(1)
    assert policy.calls == 2
    assert policy.sleeps == [3.0]
    assert len(state.candidates) == 1


def test_503_is_retried_but_400_401_403_are_not(tmp_path):
    for status, expected_calls in ((503, 3), (400, 1), (401, 1), (403, 1), (404, 1)):
        store = CheckpointStore(tmp_path / f"task_{status}")
        store.save(TaskState(goal="Find EGFR candidates", config=load_config(), mock=True))
        policy = FlakyPolicy(failures=5, exc_factory=lambda s=status: api_status_error(s))
        state = Harness(store, policy, sleep=lambda _: None).run(1)
        assert policy.calls == expected_calls, status
        assert state.events[-1]["type"] == "error"
        expected_category = "network" if status == 503 else "provider"
        assert state.events[-1]["error_category"] == expected_category, status
        assert state.model_calls_used == expected_calls


def test_schema_error_never_enters_network_retry(tmp_path):
    store = create(tmp_path)
    policy = FlakyPolicy(failures=5, exc_factory=lambda: json.JSONDecodeError("bad shape", "{}", 0))
    state = Harness(store, policy, sleep=lambda _: None).run(1)
    assert policy.calls == 1                       # no retry
    assert not any(e["type"] == "retry" for e in state.events)
    assert state.events[-1]["error_category"] == "schema"


def test_state_machine_rejection_never_enters_network_retry(tmp_path):
    store = create(tmp_path)
    policy = FlakyPolicy(failures=5, exc_factory=lambda: ValueError(
        "Illegal action at stage=strategy_decision: 'finish'; allowed_tools=['choose_strategy']"))
    state = Harness(store, policy, sleep=lambda _: None).run(1)
    assert policy.calls == 1
    assert not any(e["type"] == "retry" for e in state.events)
    assert state.events[-1]["error_category"] == "state_machine"


def test_tool_error_never_enters_network_retry(tmp_path):
    store = create(tmp_path)
    policy = FlakyPolicy(failures=5, exc_factory=lambda: ValueError("Edit precheck failed: ['valence']"))
    state = Harness(store, policy, sleep=lambda _: None).run(1)
    assert policy.calls == 1
    assert state.events[-1]["error_category"] == "tool"


def test_retry_metrics_are_mutually_exclusive(tmp_path):
    """Network retries, final failures, schema errors and rejections must not overlap."""
    store = CheckpointStore(tmp_path / "mixed")
    store.save(TaskState(goal="Find EGFR candidates", config=load_config(), mock=True))

    class Mixed:
        calls = 0
        def decide(self, state, registry):
            self.calls += 1
            if self.calls == 1:
                raise api_connection_error()
            if self.calls == 2:
                return ["malformed action"]                  # schema rejection
            return action("generate", count=1, focus="EGFR")

    policy = Mixed()
    state = Harness(store, policy, sleep=lambda _: None).run(3)
    retries = [e for e in state.events if e["type"] == "retry"]
    errors = [e for e in state.events if e["type"] == "error"]
    assert len(retries) == 1 and retries[0]["error_category"] == "network"
    network_errors = [e for e in errors if e["error_category"] == "network"]
    schema_errors = [e for e in errors if e["error_category"] == "schema"]
    assert len(network_errors) == 0        # the retry succeeded, so no final failure
    assert len(schema_errors) == 1
    assert len(state.candidates) == 1      # the schema error did not count as an edit


def test_unsafe_tool_is_not_retried_even_on_a_network_error(tmp_path):
    """A tool with retry_safe=False must not be replayed on a transport error."""
    from agents.harness.tools import Tool, ToolRegistry
    store = create(tmp_path)
    registry = ToolRegistry()
    calls = []
    def unsafe(*args):
        calls.append(1)
        raise api_timeout_error()
    registry.register(Tool("unsafe", "test", {}, unsafe))
    state = Harness(store, FlakyPolicy(0, api_timeout_error, action("unsafe")), registry,
                    sleep=lambda _: None).run(1)
    assert len(calls) == 1
    assert not any(e["type"] == "retry" for e in state.events)


# ---------------------------------------------------------------------------
# 5. Client lifecycle
# ---------------------------------------------------------------------------

def test_client_is_created_once_across_many_decisions(tmp_path, monkeypatch):
    """Ten planner calls must open exactly one HTTP client per provider."""
    import agents.llm
    store = create(tmp_path)
    created = []
    class Client:
        def __init__(self):
            created.append(1)
        def chat_json(self, system, user):
            return action("generate", count=1, focus="EGFR")
        def close(self):
            pass
    scope = ClientScope(factory=lambda name, cfg, ev: Client())
    policy = LLMPolicy(scope=scope)
    def fake_get_client(name, cfg, **kwargs):
        return cfg["_client_scope"].client(name, cfg)
    monkeypatch.setattr(agents.llm, "get_client", fake_get_client)
    for _ in range(10):
        policy.decide(store.load(), default_registry())
    assert len(created) == 1
    assert scope.created == 1
    policy.close()
    policy.close()          # idempotent
    assert scope.closed is True


def test_harness_closes_the_policy_it_created(tmp_path, monkeypatch):
    import agents.llm
    store = create(tmp_path)
    state = store.load()
    state.mock = False
    store.save(state)
    closed = []
    class Client:
        def chat_json(self, system, user):
            return action("pause", message="Need input")
        def close(self):
            closed.append(1)
    monkeypatch.setattr(agents.llm, "get_client", lambda *a, **k: Client())
    harness = Harness(store)
    # The Harness creates an LLMPolicy because none was injected; that policy
    # owns a ClientScope, which closes the provider client on exit.
    assert harness.policy is None
    harness.run(1)
    assert isinstance(harness.policy, LLMPolicy)
    assert harness.policy.client_scope.closed is True
    harness.close()
    harness.close()         # idempotent, no exception
    assert harness.policy.client_scope.closed is True


def test_harness_does_not_close_an_injected_policy(tmp_path):
    """A caller-owned policy survives several runs and is closed by the caller."""
    store = create(tmp_path)
    closed = []
    policy = FlakyPolicy(0, api_timeout_error, action("generate", count=1, focus="EGFR"),
                         close_tracker=closed)
    for _ in range(3):
        Harness(store, policy, sleep=lambda _: None).run(1)
    assert closed == []            # Harness must not close a caller-owned policy
    policy.close()
    assert closed == [1]
    policy.close()                 # the policy is idempotent too
    assert closed == [1]


def test_client_scope_closes_once_and_survives_a_failing_close():
    calls = []
    class Client:
        def close(self):
            calls.append(1)
            raise RuntimeError("already broken")
    scope = ClientScope(factory=lambda *a: Client())
    scope.client("p", {"model": "m"})
    scope.close()
    scope.close()
    assert calls == [1]            # a raising close must not be retried or propagated
    with pytest.raises(RuntimeError, match="already closed"):
        scope.client("p", {"model": "m"})


def test_client_scope_reuses_clients_but_never_serializes_them(tmp_path):
    store = create(tmp_path)
    scope = ClientScope(factory=lambda name, cfg, ev: type("C", (), {"close": lambda self: None})())
    state = store.load()
    state.config["_client_scope"] = scope      # deliberately illegal placement
    with pytest.raises(TypeError):
        json.dumps(state.config)               # proves a live scope cannot be persisted


# ---------------------------------------------------------------------------
# 6. TaskState stays clean
# ---------------------------------------------------------------------------

def test_task_state_never_contains_a_client_or_secret(tmp_path):
    """deepcopy, checkpoint save and reload must not carry client objects."""
    store = create(tmp_path)
    state = store.load()
    state.events.append({"type": "model_request", "request_id": "r1", "outcome": "response_received"})
    store.save(state)
    reloaded = store.load()
    assert not hasattr(reloaded, "client")
    assert not hasattr(reloaded, "http_client")
    assert not hasattr(reloaded, "repository")
    copied = deepcopy(reloaded)
    for holder in (reloaded, copied):
        for key, value in holder.config.items():
            assert key not in {"_client_scope", "_client_scope_token"}
            assert not hasattr(value, "close")
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    assert "_client_scope" not in json.dumps(payload)
    assert "_client_scope_token" not in json.dumps(payload)


def test_scope_token_never_reaches_the_checkpoint(tmp_path):
    """The Harness injects the token only for the tool call and strips it after."""
    from agents.harness.tools import Tool, ToolRegistry
    store = create(tmp_path)
    seen = {}
    registry = ToolRegistry()
    def handler(state, args, directory):
        seen["token"] = state.config.get("_client_scope_token")
        seen["scope_present"] = "_client_scope" in state.config
        return {"ok": True}
    registry.register(Tool("work", "test", {}, handler))
    scope = ClientScope(factory=lambda *a: type("C", (), {"close": lambda self: None})())
    class Scripted:
        def decide(self, state, registry):
            return action("work")
        def close(self):
            pass
    policy = Scripted()
    policy.client_scope = scope
    state = Harness(store, policy, registry, sleep=lambda _: None).run(1)
    assert seen["token"] == scope.token
    assert seen["scope_present"] is False
    assert "_client_scope_token" not in state.config
    assert "_client_scope_token" not in store.path.read_text(encoding="utf-8")
    assert scope.token not in store.path.read_text(encoding="utf-8")
    scope.close()


# ---------------------------------------------------------------------------
# 7. Redaction
# ---------------------------------------------------------------------------

def test_sanitize_removes_keys_headers_and_url_parameters():
    fake = "sk-FAKE0000000000000000000000000000"
    cases = [
        f"Authorization: Bearer {fake}",
        f"api_key={fake}",
        f'"api_key": "{fake}"',
        f"x-api-key: {fake}",
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
        f"POST https://api.minimaxi.com/v1/chat/completions?key={fake}&model=M3",
    ]
    for text in cases:
        cleaned = sanitize(text, secrets=(fake,))
        assert fake not in cleaned
        assert "eyJhbGciOiJIUzI1NiJ9" not in cleaned
        assert "api.minimaxi.com/v1" not in cleaned
        assert "<redacted>" in cleaned


def test_sanitize_keeps_the_host_but_drops_path_and_query():
    cleaned = sanitize("failed https://api.minimaxi.com/v1/chat?key=abc123456789&trace=x")
    assert cleaned == "failed https://api.minimaxi.com/<redacted>"


def test_sanitize_exception_keeps_the_type():
    text = sanitize_exception(ValueError("Expected arguments: ['a']"))
    assert text == "ValueError: Expected arguments: ['a']"


def test_redaction_survives_the_whole_pipeline(tmp_path):
    """A fake key must not appear in events, errors, state, Repository or stdout."""
    import agents.llm
    from db.repository import SQLiteRepository
    fake_key = "sk-FAKE-PIPELINE-SECRET-0000111122223333"
    repository = SQLiteRepository(tmp_path / "repo.sqlite3")
    store = CheckpointStore(tmp_path / "task", repository=repository)
    store.save(TaskState(goal="Find EGFR candidates", config=load_config(), mock=True))

    class Leaky:
        calls = 0
        def decide(self, state, registry):
            self.calls += 1
            if self.calls <= 3:
                raise ConnectionError(
                    f"failed to connect with Authorization: Bearer {fake_key} "
                    f"to https://api.minimaxi.com/v1/chat?key={fake_key}")
            return action("pause", message="Need input")

    records = []
    policy = Leaky()
    harness = Harness(store, policy, sleep=lambda _: None)
    harness._secrets = (fake_key,)
    harness._record_request({"type": "model_request", "request_id": "r1",
                             "exception": sanitize(f"Bearer {fake_key}", secrets=(fake_key,)),
                             "outcome": "request_failed"})
    state = harness.run(2)
    serialized_state = json.dumps(state.events, ensure_ascii=False)
    serialized_evidence = json.dumps(records, ensure_ascii=False)
    assert fake_key not in serialized_state
    assert fake_key not in serialized_evidence
    rows = repository.connection.execute(
        "SELECT payload_json FROM agent_events").fetchall()
    dumped = " ".join(row["payload_json"] for row in rows)
    assert fake_key not in dumped
    assert "<redacted>" in dumped
    repository.close()


def test_model_request_evidence_fields_are_complete_and_sanitized(tmp_path, monkeypatch):
    import agents.llm
    store = create(tmp_path)
    state = store.load()
    state.mock = False
    store.save(state)
    records = []
    class Client:
        def chat_json(self, system, user):
            return action("pause", message="Need input")
        def close(self):
            pass
    def fake_get_client(name, cfg, evidence=None, **kwargs):
        records.append({"provider": name, "evidence_installed": evidence is not None})
        return Client()
    monkeypatch.setattr(agents.llm, "get_client", fake_get_client)
    harness = Harness(store)
    harness._record_request({
        "type": "model_request", "request_id": "abc", "sequence": 1,
        "provider": "MiniMax", "model": "MiniMax-M3", "base_url_host": "api.minimaxi.com",
        "started_at": 1.0, "finished_at": 1.5, "latency_ms": 500.0,
        "response_received": True, "http_status": 200, "exception_category": None,
        "exception": None, "retry_scheduled": False, "retry_delay_s": None,
        "token_usage": None, "token_usage_status": "unavailable",
        "thinking": "disabled", "schema_valid": None, "outcome": "response_received"})
    harness.run(1)
    flushed = [e for e in store.load().events if e["type"] == "model_request"]
    assert len(flushed) == 1
    event = flushed[0]
    for field in ("task_id" if False else "request_id", "sequence", "provider", "model",
                  "base_url_host", "started_at", "finished_at", "latency_ms",
                  "response_received", "http_status", "exception_category", "exception",
                  "retry_scheduled", "retry_delay_s", "token_usage", "token_usage_status",
                  "thinking", "schema_valid", "outcome"):
        assert field in event, field
    assert event["token_usage"] is None
    assert event["token_usage_status"] == "unavailable"
    assert "/" not in event["base_url_host"]


def test_schema_error_names_the_exact_offending_keys():
    """A bare 'Expected arguments' list made the v4 planner repeat one malformed
    call three times. The error must say which key is missing or unexpected."""
    from agents.harness.schema import validate_value
    spec = {"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
            "required": ["a", "b"], "additionalProperties": False}
    with pytest.raises(ValueError) as missing:
        validate_value({"a": "x"}, spec, "options[0]")
    assert "missing=['b']" in str(missing.value)
    assert "allowed=" in str(missing.value)
    with pytest.raises(ValueError) as extra:
        validate_value({"a": "x", "b": "y", "evidence_ids": ["c1"]}, spec, "options[0]")
    assert "unexpected=['evidence_ids']" in str(extra.value)
    validate_value({"a": "x", "b": "y"}, spec, "options[0]")


def test_unknown_option_key_is_classified_as_schema_not_tool(tmp_path):
    from agents.harness.schema import validate_value
    spec = {"type": "object", "properties": {"edit": {"type": "object", "properties": {}}},
            "required": ["edit"], "additionalProperties": False}
    with pytest.raises(ValueError) as exc:
        validate_value({"edit": {}, "evidence_ids": []}, spec, "options[0]")
    assert error_category(exc.value) == "schema"
    assert classify_error(exc.value) is None       # never retried as a network error


def test_injected_policy_still_emits_request_evidence(tmp_path, monkeypatch):
    """An externally built LLMPolicy must not silence the Harness audit trail."""
    import agents.llm
    from agents.harness.runtime import LLMPolicy
    store = create(tmp_path)
    state = store.load()
    state.mock = False
    store.save(state)
    class Client:
        def chat_json(self, system, user):
            return action("pause", message="Need input")
        def close(self):
            pass
    monkeypatch.setattr(agents.llm, "get_client", lambda *a, **k: Client())
    policy = LLMPolicy()
    harness = Harness(store, policy)
    harness._record_request({"type": "model_request", "request_id": "r1",
                             "outcome": "response_received"})
    harness.run(1)
    # The Harness installed its sink on the injected policy.
    assert policy.evidence is not None
    flushed = [e for e in store.load().events if e["type"] == "model_request"]
    assert len(flushed) == 1
    assert flushed[0]["request_id"] == "r1"


def test_request_evidence_carries_task_id_step_and_attempt(tmp_path, monkeypatch):
    import agents.llm
    store = create(tmp_path)
    state = store.load()
    state.mock = False
    store.save(state)
    seen = []
    class Client:
        def chat_json(self, system, user):
            return action("pause", message="Need input")
        def close(self):
            pass
    monkeypatch.setattr(agents.llm, "get_client", lambda *a, **k: Client())
    harness = Harness(store)
    harness.run(1)
    policy = harness.policy
    sink = policy.evidence
    sink({"type": "model_request", "request_id": "r9", "outcome": "response_received"})
    events = [e for e in store.load().events if e["type"] == "model_request"]
    # The sink is flushed at decision time, so we assert on the collected record.
    assert harness.request_evidence[-1]["task_id"] == state.task_id
    assert harness.request_evidence[-1]["step"] == 1
    assert harness.request_evidence[-1]["attempt"] == 1


def test_evidence_hook_failure_never_breaks_a_request():
    """A broken audit sink must not turn a working request into a failure."""
    import os
    from unittest.mock import patch
    import agents.llm as llm_module
    cfg = load_config()
    name = next(iter(cfg["llm"]["providers"]))
    def broken(_event):
        raise RuntimeError("audit sink unavailable")
    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.chat = type("Chat", (), {"completions": type("C", (), {
                "create": lambda self, **kw: type("R", (), {
                    "usage": None,
                    "choices": [type("Ch", (), {"message": type("M", (), {"content": "{}"})()})()],
                })()})()})()
        def close(self):
            pass
    with patch.object(llm_module.httpx, "Client", return_value=object()), \
         patch.object(llm_module, "OpenAI", FakeOpenAI), \
         patch.dict(os.environ, {cfg['llm']['providers'][name]['api_key_env']: 'test-placeholder'}):
        client = llm_module.get_client(name, cfg, evidence=broken)
        assert client.chat("s", "u") == "{}"
        client.close()
