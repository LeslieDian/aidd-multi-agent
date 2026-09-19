"""Offline tests for the MiniMax connectivity acceptance script.

These tests never touch the network: the client factory is injected. They lock
down the contract the real run must satisfy — three sequential requests, one
client, explicit close, schema validation, evidence recording and no key leak.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agents.redaction import sanitize
from scripts.check_minimax_connectivity import run, request_once, _redaction_ok


class FakeClient:
    """Records calls and returns the fixed transport schema."""

    def __init__(self, failures=0, malformed=False, api_key="sk-FAKE-CONNECTIVITY-000011112222"):
        self.failures = failures
        self.malformed = malformed
        self.api_key = api_key
        self.calls = []
        self.closed = 0
        self.evidence = None

    def chat_json(self, system, user):
        self.calls.append(user)
        if len(self.calls) <= self.failures:
            raise TimeoutError(f"temporary failure with Bearer {self.api_key}")
        if self.malformed:
            return {"status": "wrong", "sequence": -1}
        sequence = int(user.split("=")[1])
        if self.evidence is not None:
            self.evidence({"type": "model_request", "request_id": f"r{len(self.calls)}",
                           "provider": "MiniMax", "model": "MiniMax-M3",
                           "base_url_host": "api.minimaxi.com", "outcome": "response_received",
                           "token_usage": None, "token_usage_status": "unavailable"})
        return {"status": "ok", "sequence": sequence}

    def close(self):
        self.closed += 1


def factory(client):
    def build(name, cfg, evidence):
        client.evidence = evidence
        return client
    return build


def test_three_sequential_requests_pass_the_gate(tmp_path):
    client = FakeClient()
    summary = run(tmp_path / "summary.json", client_factory=factory(client))
    assert summary["gate"] == "passed"
    assert summary["attempted"] == 3
    assert summary["successful"] == 3
    assert summary["schema_valid"] == 3
    assert client.calls == ["sequence=1", "sequence=2", "sequence=3"]
    assert summary["clients_created"] == 1
    assert summary["client_reused_for_all_requests"] is True
    assert summary["client_closed_explicitly"] is True
    assert client.closed == 1
    assert summary["retried_requests"] == 0
    assert summary["total_attempts"] == 3
    assert all(r["latency_ms"] >= 0 for r in summary["requests"])
    assert summary["request_evidence_records"] == 3
    assert summary["token_usage"] == "unavailable"
    assert summary["monetary_cost"] == "unavailable"
    assert (tmp_path / "summary.json").exists()


def test_transport_failure_fails_the_gate_and_never_claims_success(tmp_path):
    client = FakeClient(failures=99)
    summary = run(tmp_path / "summary.json", client_factory=factory(client), attempts=2)
    assert summary["gate"] == "failed"
    assert summary["successful"] == 0
    assert all(r["attempts"] == 2 and r["retried"] is True for r in summary["requests"])
    assert all(r["error_category"] == "network" for r in summary["requests"])
    assert summary["retried_requests"] == 3


def test_schema_mismatch_is_reported_not_hidden(tmp_path):
    client = FakeClient(malformed=True)
    summary = run(tmp_path / "summary.json", client_factory=factory(client))
    assert summary["successful"] == 3          # the request itself succeeded
    assert summary["schema_valid"] == 0        # but the payload did not match
    assert all(r["schema_valid"] is False for r in summary["requests"])
    assert summary["gate"] == "passed"         # the gate tracks request success


def test_connectivity_summary_never_contains_the_api_key(tmp_path):
    client = FakeClient()
    summary = run(tmp_path / "summary.json", client_factory=factory(client))
    blob = json.dumps(summary, ensure_ascii=False)
    assert client.api_key not in blob
    assert "Bearer" not in blob
    assert "Authorization" not in blob
    assert summary["redaction_check"]["api_key_absent_from_evidence"] is True
    assert summary["redaction_check"]["base_url_host_only"] is True


def test_redaction_helper_detects_a_leak():
    client = FakeClient()
    leaking = [{"exception": f"Bearer {client.api_key}"}]
    assert _redaction_ok(leaking, client) is False
    clean = [{"exception": sanitize(f"Bearer {client.api_key}", (client.api_key,))}]
    assert _redaction_ok(clean, client) is True


def test_request_once_validates_the_returned_sequence():
    client = FakeClient()
    outcome = request_once(client, 2)
    assert outcome["sequence"] == 2
    assert outcome["schema_valid"] is True
    assert outcome["returned_sequence"] == 2
    assert outcome["latency_ms"] >= 0
