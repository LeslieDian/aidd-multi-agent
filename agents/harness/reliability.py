"""Explicit resource accounting, client lifetime and bounded retries.

The Harness is the single owner of retry policy. Neither the business tools
nor the model SDK retry on their own:

* the OpenAI SDK is configured with ``max_retries=0`` (see ``agents.llm``);
* every provider call inside one task reuses one HTTP client created once
  (``ClientScope``) and that client is closed explicitly when the task ends;
* only *transport* failures are retried, with bounded exponential backoff and
  jitter, honouring a legal ``Retry-After`` up to a configured ceiling.

Error taxonomy (kept stable, it is reported in the v4 summary):

``network``        connection/timeout/remote disconnect, HTTP 429, 5xx
``schema``         model returned JSON that does not match the required shape
``state_machine``  action is illegal for the current stage or constraints
``tool``           tool argument error, RDKit edit failure, unknown defect
"""
from __future__ import annotations

import email.utils
import random
from copy import deepcopy
from datetime import datetime, timezone
import time
from uuid import uuid4


class BudgetExceeded(Exception):
    pass


# Process-local registry mapping an opaque scope token to its live ClientScope.
# Tokens contain no credentials and are never persisted as scope objects.
_SCOPES: dict = {}


# HTTP statuses that are temporary transport/server conditions.
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
# Explicitly non-retryable client errors: retrying burns quota for nothing.
PERMANENT_STATUS = {400, 401, 403, 404}


def client_config(config):
    """Return a copy of ``config`` with Harness-owned timeout/retry settings applied.

    The SDK retry count is forced to 0 so the Harness retry loop is the only
    retry mechanism, and no client object is ever placed into the config.
    """
    result = deepcopy(config)
    timeout = result.get("harness", {}).get("request_timeout", 60)
    if not isinstance(timeout, (int, float)) or not 1 <= timeout <= 300:
        raise ValueError("harness.request_timeout must be between 1 and 300 seconds")
    for provider in result.get("llm", {}).get("providers", {}).values():
        provider.update(timeout=timeout, max_retries=0)
    return result


def classify_error(exc) -> str | None:
    """Return ``"network"`` for retryable transport errors, else ``None``.

    Deliberately narrow. Schema errors, illegal actions, tool argument errors,
    RDKit failures, constraint violations, HTTP 400/401/403/404 and unknown
    ordinary exceptions all return ``None`` and are never retried.
    """
    from openai import APIConnectionError, APIStatusError

    if isinstance(exc, APIStatusError):
        status = getattr(exc, "status_code", None)
        if status in RETRYABLE_STATUS:
            return "network"
        return None  # 400/401/403/404 and anything else: permanent
    if isinstance(exc, APIConnectionError):
        # openai maps httpx transport errors (connect/read timeouts, remote
        # disconnect) onto this class.
        return "network"
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return "network"
    try:
        import httpx
    except ImportError:  # pragma: no cover - httpx is a hard dependency
        return None
    if isinstance(exc, httpx.TransportError):
        return "network"
    return None


def transient(exc) -> bool:
    """Backwards-compatible predicate: is this exception a retryable network error?"""
    return classify_error(exc) == "network"


def error_category(exc) -> str:
    """Map an exception to the documented audit categories."""
    import json

    from openai import APIStatusError

    if classify_error(exc) == "network":
        return "network"
    if isinstance(exc, (json.JSONDecodeError, UnicodeDecodeError)):
        return "schema"
    if isinstance(exc, APIStatusError):
        # A non-retryable HTTP status is still a provider response, not a
        # state-machine rejection and not a tool defect.
        return "provider"
    message = f"{type(exc).__name__}: {exc}"
    if ("Expected arguments" in message or "invalid keys" in message
            or "Action must contain" in message or "Action needs" in message):
        return "schema"
    if "Illegal action" in message or "hypothesis" in message or "stage=" in message:
        return "state_machine"
    return "tool"


def retry_after_seconds(exc) -> float | None:
    """Return a legal ``Retry-After`` value in seconds, or ``None``.

    Accepts the delta-seconds form and the HTTP-date form. Negative or
    unparsable values are ignored rather than trusted.
    """
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    try:
        raw = headers.get("retry-after")
    except Exception:
        return None
    if raw is None:
        return None
    raw = str(raw).strip()
    if not raw:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
        if when is None:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        seconds = (when - datetime.now(timezone.utc)).total_seconds()
    if seconds < 0:
        return None
    return seconds


class RetryPolicy:
    """Bounded exponential backoff with jitter, owned by the Harness.

    ``max_attempts`` is the total number of attempts (not extra retries) and is
    capped by ``harness.max_attempts`` (1..5). The delay before attempt *n* is
    ``min(base * 2**(n-1), max_delay)`` plus up to ``jitter * delay`` random
    jitter. A usable ``Retry-After`` from the provider wins, but is still
    clamped to ``max_delay``. Sleeping is injected so tests never wait.
    """

    def __init__(self, max_attempts=3, base_delay=1.0, max_delay=30.0, jitter=0.25,
                 sleep=time.sleep, random_source=random.random):
        if type(max_attempts) is not int or not 1 <= max_attempts <= 5:
            raise ValueError("harness.max_attempts must be between 1 and 5")
        for name, value in (("retry_base_delay", base_delay), ("retry_max_delay", max_delay)):
            if not isinstance(value, (int, float)) or value < 0 or value > 300:
                raise ValueError(f"harness.{name} must be between 0 and 300 seconds")
        if not isinstance(jitter, (int, float)) or not 0 <= jitter <= 1:
            raise ValueError("harness.retry_jitter must be between 0 and 1")
        self.max_attempts = max_attempts
        self.base_delay = float(base_delay)
        self.max_delay = float(max_delay)
        self.jitter = float(jitter)
        self.sleep = sleep
        self.random_source = random_source

    @classmethod
    def from_config(cls, config, sleep=time.sleep, random_source=random.random):
        harness = (config or {}).get("harness", {}) or {}
        return cls(
            max_attempts=harness.get("max_attempts", 3),
            base_delay=harness.get("retry_base_delay", 1.0),
            max_delay=harness.get("retry_max_delay", 30.0),
            jitter=harness.get("retry_jitter", 0.25),
            sleep=sleep,
            random_source=random_source,
        )

    def delay(self, attempt: int, exc=None) -> float:
        """Delay before attempt ``attempt + 1``; always within ``[0, max_delay]``."""
        backoff = min(self.base_delay * (2 ** (attempt - 1)), self.max_delay)
        jittered = backoff + self.random_source() * self.jitter * backoff
        hinted = retry_after_seconds(exc) if exc is not None else None
        chosen = hinted if hinted is not None else jittered
        return round(min(chosen, self.max_delay), 6)


class ClientScope:
    """One HTTP client per provider, reused for a whole task and closed once.

    The scope object itself is deliberately *not* stored in ``TaskState``: it
    holds live sockets, so it must not be deep-copied, checkpointed or hashed.
    Code that needs to reach the scope from a tool (which only receives the
    task state) passes the scope's opaque string ``token`` instead. A token is
    a random identifier with no credential content, so even if one were written
    into a checkpoint it would leak nothing; ``_SCOPES`` is a process-local
    registry and the token is resolved back to the live scope here.
    """

    def __init__(self, factory=None):
        self._clients: dict = {}
        self._factory = factory
        self._closed = False
        self.created = 0
        self.token = uuid4().hex
        _SCOPES[self.token] = self

    @property
    def closed(self) -> bool:
        return self._closed

    def client(self, provider_name, provider_config, evidence=None, api_key=None):
        """Return the cached client for ``provider_name``, creating it once."""
        if self._closed:
            raise RuntimeError("Client scope is already closed")
        existing = self._clients.get(provider_name)
        if existing is not None:
            if evidence is not None:
                existing.evidence = evidence
            return existing
        from agents.llm import LLMClient
        builder = self._factory or (
            lambda name, cfg, ev: LLMClient(cfg, api_key=api_key, evidence=ev))
        client = builder(provider_name, provider_config, evidence)
        self._clients[provider_name] = client
        self.created += 1
        return client

    def close(self) -> None:
        """Close every owned client. Idempotent and never raises."""
        if self._closed:
            return
        self._closed = True
        _SCOPES.pop(self.token, None)
        for client in self._clients.values():
            closer = getattr(client, "close", None) or getattr(client, "aclose", None)
            if closer is None:
                continue
            try:
                closer()
            except Exception:
                pass
        self._clients = {}

    def __enter__(self) -> "ClientScope":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


def scope_for(token):
    """Resolve a scope token back to its live scope, or ``None`` if unknown."""
    if not isinstance(token, str):
        return None
    return _SCOPES.get(token)


def reserve(state, model_calls=0, evaluations=0):
    if state.model_calls_used + model_calls > state.max_model_calls:
        raise BudgetExceeded("model_call_budget_exhausted")
    if state.evaluations_used + evaluations > state.max_evaluations:
        raise BudgetExceeded("evaluation_budget_exhausted")
    state.model_calls_used += model_calls
    state.evaluations_used += evaluations
