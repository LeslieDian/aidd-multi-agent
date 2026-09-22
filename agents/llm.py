"""agents/llm.py - OpenAI-compatible LLM client (provider-agnostic).

Works against any OpenAI-compatible chat completions endpoint by swapping
base_url / model / extra_body in config.yaml. Active providers are
configured under `llm.providers` and selected via `llm.generators` and
`llm.judge`.

API keys are loaded from environment (.env). They are NEVER read from
config.yaml or any tracked file, and they are NEVER written to events,
checkpoints, the Repository or logs (see agents/redaction.py).

Client lifetime
---------------
One :class:`LLMClient` owns exactly one ``httpx.Client`` and one SDK client.
It is created once per policy/task scope and closed explicitly through
:meth:`LLMClient.close`, which is idempotent. The client object is never part
of ``TaskState``, so it is never deep-copied, checkpointed or hashed.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from openai import OpenAI
import httpx

from .redaction import sanitize, sanitize_exception

# Load .env if present (no-op if python-dotenv is missing)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Documented SDK contract: the Harness owns retries, the SDK must not retry.
SDK_MAX_RETRIES = 0


def base_url_host(base_url: str) -> str:
    """Return only the host of a base URL; never the path, query or credentials."""
    parsed = urlparse(base_url if "//" in base_url else f"//{base_url}")
    return parsed.hostname or ""


class LLMClient:
    """Thin wrapper around OpenAI SDK for OpenAI-compatible providers.

    ``evidence`` is an optional callable ``(event: dict) -> None`` used to
    record one auditable, sanitized request record per HTTP attempt.
    """

    def __init__(self, provider_config: dict, api_key: str | None = None, evidence=None):
        self.cfg = provider_config
        env_var = provider_config["api_key_env"]
        self.api_key = api_key or os.getenv(env_var)
        if not self.api_key:
            raise EnvironmentError(
                f"Missing API key: set {env_var} in .env (see docs/SECURITY.md)"
            )
        # 2026-09-21: optional fallback chain (api_key_env_fallbacks). When
        # the current key returns 429, the next env var in the list is
        # tried. Keys are looked up from env at switch time so the file
        # change picks up a new value without code changes.
        fallback_envs = list(provider_config.get("api_key_env_fallbacks") or [])
        self.fallback_envs = [env for env in fallback_envs if env]
        self.fallback_index = -1  # -1 means "current key is the primary"
        self.fallback_keys = {}
        for env in self.fallback_envs:
            value = os.getenv(env)
            if value:
                self.fallback_keys[env] = value
        self.evidence = evidence
        self.provider = provider_config.get("provider_name", "unknown")
        self.model_name = provider_config["model"]
        self._secrets = (self.api_key,) + tuple(self.fallback_keys.values())
        self._closed = False
        self.last_usage: dict = {}
        self.http_client = httpx.Client(
            trust_env=bool(provider_config.get("trust_env_proxy", False)),
            verify=True,
        )
        self.client = OpenAI(
            base_url=provider_config["base_url"],
            api_key=self.api_key,
            http_client=self.http_client,
            **{key: provider_config[key] for key in ("timeout", "max_retries") if key in provider_config},
        )

    @property
    def model(self) -> str:
        return self.cfg["model"]

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        """Close the SDK and HTTP clients. Idempotent; never raises."""
        if self._closed:
            return
        self._closed = True
        for attribute in ("client", "http_client"):
            target = getattr(self, attribute, None)
            closer = getattr(target, "close", None) or getattr(target, "aclose", None)
            if closer is None:
                continue
            try:
                closer()
            except Exception:  # closing must never mask the original failure
                pass

    def _maybe_swap_key(self, *, reason: str) -> bool:
        """Switch to the next key in api_key_env_fallbacks. Returns True if a
        swap happened, False if no fallback is available. Re-closes the
        current SDK + HTTP client, rebuilds OpenAI client with the new key,
        and records a structured ``key_swap`` evidence record."""
        # Refresh env-resolved fallback keys so file edits pick up.
        self.fallback_keys = {env: os.getenv(env) for env in self.fallback_envs if os.getenv(env)}
        # Find the next index we haven't tried yet.
        while self.fallback_index + 1 < len(self.fallback_envs):
            self.fallback_index += 1
            env = self.fallback_envs[self.fallback_index]
            new_key = self.fallback_keys.get(env)
            if not new_key:
                continue
            previous = self.api_key
            try:
                target = getattr(self, "client", None)
                closer = getattr(target, "close", None)
                if closer is not None:
                    closer()
                target = getattr(self, "http_client", None)
                closer = getattr(target, "close", None)
                if closer is not None:
                    closer()
            except Exception:
                pass
            self.api_key = new_key
            self._secrets = (self.api_key,) + tuple(
                v for k, v in self.fallback_keys.items() if k != env
            )
            self.http_client = httpx.Client(
                trust_env=bool(self.cfg.get("trust_env_proxy", False)),
                verify=True,
            )
            self.client = OpenAI(
                base_url=self.cfg["base_url"],
                api_key=self.api_key,
                http_client=self.http_client,
                **{
                    key: self.cfg[key] for key in ("timeout", "max_retries") if key in self.cfg
                },
            )
            self._record({
                "type": "key_swap",
                "provider": self.provider,
                "from_env": "primary" if self.fallback_index == 0 else self.fallback_envs[self.fallback_index - 1],
                "to_env": env,
                "reason": reason,
            })
            return True
        return False

    def __enter__(self) -> "LLMClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def _record(self, event: dict) -> None:
        """Best-effort evidence hook; an audit failure must not break the call."""
        if self.evidence is None:
            return
        try:
            self.evidence(event)
        except Exception:
            pass

    def _request_evidence(self, started_ns: int, sequence: int, **fields) -> None:
        finished_ns = time.time_ns()
        event = {
            "type": "model_request",
            "request_id": uuid4().hex,
            "sequence": sequence,
            "provider": self.provider,
            "model": self.model_name,
            "base_url_host": base_url_host(self.cfg["base_url"]),
            "started_at": started_ns / 1e9,
            "finished_at": finished_ns / 1e9,
            "latency_ms": round((finished_ns - started_ns) / 1e6, 3),
            "thinking": (self.cfg.get("extra_body", {}) or {}).get("thinking", {}).get("type"),
            "response_received": False,
            "http_status": None,
            "exception_category": None,
            "exception": None,
            "retry_scheduled": False,
            "retry_delay_s": None,
            "token_usage": None,
            "token_usage_status": "unavailable",
            "schema_valid": None,
            "outcome": "unknown",
        }
        event.update(fields)
        if event["exception"] is not None:
            event["exception"] = sanitize(event["exception"], self._secrets)
        self._record(event)

    def chat(
        self,
        system: str,
        user: str,
        json_mode: bool = False,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **extra,
    ) -> str:
        """Call chat completion, return raw text content.

        Exactly one sanitized ``model_request`` evidence record is emitted per
        HTTP attempt. Prompts and full responses are NOT duplicated into the
        evidence record; only a digest of the prompt is kept.
        """
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        kwargs: dict[str, Any] = {
            "model": self.cfg["model"],
            "messages": messages,
            "temperature": temperature if temperature is not None else self.cfg.get("temperature", 0.7),
            "max_tokens": max_tokens or self.cfg.get("max_tokens", 2048),
        }
        if json_mode:
            # OpenAI-compatible JSON mode
            kwargs["response_format"] = {"type": "json_object"}
        # Provider-specific extra body (e.g., MiniMax thinking control)
        if "extra_body" in self.cfg:
            kwargs["extra_body"] = {**self.cfg["extra_body"], **extra.pop("extra_body", {})}
        if extra:
            kwargs.update(extra)

        self._request_sequence = getattr(self, "_request_sequence", 0) + 1
        sequence = self._request_sequence
        started_ns = time.time_ns()
        try:
            response = self.client.chat.completions.create(**kwargs)
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            safe = sanitize_exception(exc, self._secrets)
            # Attach the redacted rendering so every downstream layer (Harness
            # events, CLI output, Repository, pytest capture) has a safe string
            # without re-deriving it from the raw provider message.
            try:
                exc.sanitized_message = safe
            except Exception:
                pass
            # 2026-09-21: account-level rate-limit fallback. If the primary
            # key returned 429 we close the current SDK/HTTP pair, swap to
            # the next env-var key in `api_key_env_fallbacks`, rebuild the
            # OpenAI client, and retry the same request exactly once. The
            # retry uses a fresh request_sequence so each HTTP attempt is
            # recorded separately. If no fallback is configured or all
            # fallbacks are exhausted, the original exception is re-raised.
            switched = False
            if status == 429 and not self._closed:
                switched = self._maybe_swap_key(reason="rate_limit_error")
            if switched:
                self._request_sequence += 1
                sequence = self._request_sequence
                started_ns = time.time_ns()
                try:
                    response = self.client.chat.completions.create(**kwargs)
                except Exception as exc2:
                    status2 = getattr(exc2, "status_code", None)
                    safe2 = sanitize_exception(exc2, self._secrets)
                    self._request_evidence(
                        started_ns, sequence,
                        response_received=False,
                        http_status=status2,
                        exception_category="network" if _is_network_error(exc2) else "sdk",
                        exception=safe2,
                        outcome="request_failed_after_key_switch",
                    )
                    raise
            else:
                self._request_evidence(
                    started_ns, sequence,
                    response_received=False,
                    http_status=status,
                    exception_category="network" if _is_network_error(exc) else "sdk",
                    exception=safe,
                    outcome="request_failed",
                )
                raise
        usage = response.usage.model_dump() if response.usage else {}
        self.last_usage = usage
        self._request_evidence(
            started_ns, sequence,
            response_received=True,
            http_status=200,
            token_usage=usage or None,
            token_usage_status="reported" if usage else "unavailable",
            outcome="response_received",
        )
        return response.choices[0].message.content or ""

    def chat_json(self, system: str, user: str, **kwargs) -> Any:
        """Call chat and parse JSON response.

        Parse failures are *schema* errors: they must never be retried as
        network errors and are reported with ``schema_valid=False``.
        """
        text = self.chat(system, user, json_mode=True, **kwargs)
        try:
            return _parse_json(text)
        except Exception as exc:
            self._request_evidence(
                time.time_ns(), getattr(self, "_request_sequence", 0),
                response_received=True,
                http_status=200,
                exception_category="schema",
                exception=sanitize_exception(exc, self._secrets),
                schema_valid=False,
                outcome="schema_error",
            )
            raise


def _is_network_error(exc) -> bool:
    """Transport-level failure: connection, timeout or a retryable HTTP status."""
    from openai import APIConnectionError, APIStatusError
    if isinstance(exc, (APIConnectionError, TimeoutError, ConnectionError)):
        return True
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, APIStatusError):
        return exc.status_code == 429 or exc.status_code >= 500
    return False


def _parse_json(text: str) -> Any:
    """Parse the first complete JSON value, tolerating code fences and prose."""
    text = text.strip()
    if text.startswith("```"):
        # strip first and last ```
        lines = text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines)
    decoder = json.JSONDecoder()
    last_error = None
    for match in re.finditer(r"[\[{]", text):
        try:
            value, _ = decoder.raw_decode(text[match.start():])
            return value
        except json.JSONDecodeError as exc:
            last_error = exc
    if last_error:
        raise last_error
    raise json.JSONDecodeError("No JSON value in model response", text, 0)


# --------- Mock client for offline testing ---------

# Stable, drug-like, RDKit-parseable molecules the mock generator cycles
# through. Kept as a module constant so the mock can satisfy any requested
# candidate count instead of a hard-coded five.
_MOCK_BASE_SMILES = [
    "CC(=O)Oc1ccccc1C(=O)O",          # aspirin
    "CC(C)Cc1ccc(C(C)C(=O)O)cc1",     # ibuprofen
    "Cn1cnc2c1c(=O)n(C)c(=O)n2C",     # caffeine
    "OC(=O)C1CCCCC1",                 # cyclohexanecarboxylic acid
    "CCO",                            # ethanol
]
# Refuse absurd counts so a typo in config cannot spin the mock forever.
_MOCK_MAX_SMILES = 200


class MockLLMClient:
    """Deterministic mock used when API keys are missing or in CI."""

    def __init__(self, provider_config: dict | None = None):
        self.cfg = provider_config or {"model": "mock"}
        self.provider = (provider_config or {}).get("provider_name", "mock")
        self.last_usage: dict = {}
        self._closed = False

    @property
    def model(self) -> str:
        return self.cfg["model"]

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        """Idempotent no-op so the mock satisfies the same lifecycle contract."""
        self._closed = True

    def __enter__(self) -> "MockLLMClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def chat(self, system: str, user: str, json_mode: bool = False, **kwargs) -> str:
        if json_mode:
            # Phase 4.2: include reflection/confidence/adopted_count so mock
            # behaves like a real Phase 4.2 judge (for tests).
            # Detect judge vs generator from system prompt keywords.
            if "experienced medicinal chemist leading" in system:
                # Judge-style response
                return json.dumps({
                    "focus": "Add a morpholine to improve aqueous solubility and Vina binding.",
                    "best_index": 0,
                    "weakness": "All candidates lack a solubilizing group on the western aryl ring.",
                    "expected_change": "logP -0.5",
                    "reasoning": "Adding morpholine should drop logP and improve solubility per erlotinib SAR.",
                    "reflection": "Previous round added morpholine and Vina improved by 0.3, so keep that direction.",
                    "confidence": 0.7,
                    "adopted_count": 2,
                })
            # Default: generator-style response (SMILES list).
            # The requested count is carried in the system prompt
            # ("Provide exactly __N__ SMILES"). Honour it: a hard-coded list of
            # five made every --mock run fail closed with
            # "candidate_count=5 expected=15" and stop after zero rounds, because
            # loop.candidates_per_round_per_generator was raised to 15 in
            # Phase 4.4 (2026-09-14).
            match = re.search(r"Provide exactly\s+(\d+)\s+SMILES", system)
            requested = int(match.group(1)) if match else len(_MOCK_BASE_SMILES)
            requested = max(1, min(requested, _MOCK_MAX_SMILES))
            # Cycle deterministically; downstream exact deduplication then has
            # something real to collapse, which is itself useful in smoke tests.
            smiles = [_MOCK_BASE_SMILES[i % len(_MOCK_BASE_SMILES)]
                      for i in range(requested)]
            return json.dumps({
                "smiles_list": smiles,
                "rationale": f"Mock: {len(smiles)} stable drug-like molecules.",
            })
        return "Mock response for: " + user[:60]

    def chat_json(self, system: str, user: str, **kwargs) -> Any:
        return json.loads(self.chat(system, user, json_mode=True, **kwargs))


def get_client(provider_name: str, config: dict, mock: bool = False, evidence=None,
               api_key: str | None = None) -> LLMClient | MockLLMClient:
    """Factory: return LLMClient or MockLLMClient.

    Only explicit `mock=True` enables MockLLMClient; missing keys fail closed.

    Callers that own a longer-lived scope should build the client once and
    reuse it (see ``agents.harness.reliability.ClientScope``); this factory
    always returns a *new* client so it stays safe for one-shot use.
    """
    providers = config.get("llm", {}).get("providers", {})
    if provider_name not in providers:
        raise KeyError(f"Unknown provider: {provider_name}. Available: {list(providers)}")
    provider_cfg = {
        **providers[provider_name],
        "provider_name": provider_name,
        "trust_env_proxy": config.get("llm", {}).get("trust_env_proxy", False),
        # Harness-owned retries: the SDK must never retry behind our back.
        "max_retries": SDK_MAX_RETRIES,
    }
    provider_cfg.pop("_client_scope", None)

    if mock:
        return MockLLMClient(provider_cfg)
    scope = config.get("_client_scope")
    if scope is None and config.get("_client_scope_token"):
        # A token is a plain string, so it survives deepcopy and JSON round-trips
        # while the live scope (which holds sockets) stays out of the state.
        from .harness.reliability import scope_for
        scope = scope_for(config["_client_scope_token"])
    if scope is not None:
        return scope.client(provider_name, provider_cfg, evidence=evidence)
    return LLMClient(provider_cfg, api_key=api_key, evidence=evidence)
