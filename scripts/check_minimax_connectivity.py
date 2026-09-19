"""Minimal real-connection acceptance for the MiniMax transport layer.

Purpose (phase 5 of the 2026-09-19 transport repair): prove that the client
lifecycle, TLS/proxy policy, request success rate, JSON schema parsing, explicit
close and request-evidence recording all work against the real endpoint.

Scope limits, enforced in code:
* three sequential requests only — never concurrent;
* a fixed trivial JSON schema (``{"status": "ok", "sequence": N}``);
* no chemistry, no docking, no Harness, no molecular experiment.

The model is asked for no chemical reasoning at all. A failure here means the
transport is unusable, so the v4 molecular experiment must NOT start.

Output: a small machine-readable summary (default
``runs/samples/minimax_connectivity_20260919_summary.json``). Request logs and
full responses are never written; only counts, latencies, attempt numbers,
whether a retry happened and redaction checks.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml

from agents.llm import LLMClient, base_url_host
from agents.redaction import sanitize
from agents.harness.reliability import ClientScope, RetryPolicy, classify_error, client_config

SCHEMA = {"status": "ok", "sequence": None}
SYSTEM = (
    "You are a transport self-test endpoint. Reply with STRICT JSON only, no prose, no code fences. "
    'The object must be exactly {"status": "ok", "sequence": <the integer given by the user>}. '
    "Do not add, rename or omit keys. Do not explain."
)


def request_once(client, sequence: int) -> dict:
    """One request; return only non-sensitive, auditable fields."""
    import time
    started = time.time_ns()
    payload = client.chat_json(SYSTEM, f"sequence={sequence}")
    finished = time.time_ns()
    valid = isinstance(payload, dict) and payload.get("status") == "ok" and payload.get("sequence") == sequence
    return {
        "sequence": sequence,
        "latency_ms": round((finished - started) / 1e6, 3),
        "schema_valid": bool(valid),
        "returned_sequence": payload.get("sequence") if isinstance(payload, dict) else None,
    }


def run(output, attempts=None, client_factory=None):
    config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    planner_name = config.get("harness", {}).get("planner") or config["llm"]["judge"]
    planner = config["llm"]["providers"][planner_name]
    effective = client_config(config)
    retry = RetryPolicy.from_config(effective, sleep=lambda _: None)
    attempts = retry.max_attempts if attempts is None else attempts

    records: list[dict] = []
    scope = ClientScope(factory=client_factory)
    results = []
    try:
        client = scope.client(planner_name, {
            **effective["llm"]["providers"][planner_name],
            "provider_name": planner_name,
            "trust_env_proxy": config.get("llm", {}).get("trust_env_proxy", False),
            "max_retries": 0,
        }, evidence=records.append)
        for sequence in (1, 2, 3):
            outcome = {"sequence": sequence, "attempts": 0, "retried": False,
                       "succeeded": False, "error_category": None}
            for attempt in range(1, attempts + 1):
                outcome["attempts"] = attempt
                try:
                    outcome.update(request_once(client, sequence))
                    outcome["succeeded"] = True
                    break
                except Exception as exc:
                    category = classify_error(exc)
                    outcome["error_category"] = category or "non_network"
                    outcome["error"] = sanitize(f"{type(exc).__name__}: {exc}",
                                                (getattr(client, "api_key", None),))
                    if category != "network" or attempt == attempts:
                        break
                    outcome["retried"] = True
            results.append(outcome)
    finally:
        clients_created = scope.created
        scope.close()

    summary = {
        "schema_version": 1,
        "experiment": "minimax_connectivity_20260919",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "purpose": "Verify model transport lifecycle before any v4 molecular run",
        "transport": {
            "provider": planner_name,
            "base_url_host": base_url_host(planner["base_url"]),
            "model": planner["model"],
            "timeout_seconds": effective.get("harness", {}).get("request_timeout", 60),
            "max_sdk_retries": 0,
            "trust_env_proxy": config.get("llm", {}).get("trust_env_proxy", False),
            "tls_verify": True,
            "thinking": planner.get("extra_body", {}).get("thinking", {}).get("type"),
            "max_attempts": attempts,
            "retry_base_delay": retry.base_delay,
            "retry_max_delay": retry.max_delay,
            "retry_jitter": retry.jitter,
        },
        "schema": {"status": "ok", "sequence": "1..3"},
        "attempted": len(results),
        "successful": sum(r["succeeded"] for r in results),
        "schema_valid": sum(bool(r.get("schema_valid")) for r in results),
        "clients_created": clients_created,
        "client_reused_for_all_requests": clients_created == 1 and len(results) == 3,
        "client_closed_explicitly": scope.closed,
        "retried_requests": sum(r["retried"] for r in results),
        "total_attempts": sum(r["attempts"] for r in results),
        "requests": results,
        "request_evidence_records": len(records),
        "request_evidence_outcomes": sorted({r.get("outcome") for r in records if r.get("outcome")}),
        "redaction_check": {
            "checked": True,
            "api_key_absent_from_evidence": _redaction_ok(records, client),
            "base_url_host_only": all("/" not in str(r.get("base_url_host", "")) for r in records),
        },
        "token_usage": "unavailable",
        "monetary_cost": "unavailable",
        "gate": "passed" if all(r["succeeded"] for r in results) else "failed",
    }
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    Path(output).write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False),
                            encoding="utf-8")
    return summary


def _redaction_ok(records, client) -> bool:
    """Assert no key, header value or full URL survived into the evidence records.

    The check is about *credential material*, not about the word "Bearer"
    appearing: ``Bearer <redacted>`` is exactly the intended safe rendering.
    """
    import re
    key = getattr(client, "api_key", None)
    blob = json.dumps(records, ensure_ascii=False)
    if key and key in blob:
        return False
    if re.search(r"(?i)bearer\s+(?!<redacted>)[A-Za-z0-9._\-]{8,}", blob):
        return False
    if re.search(r"(?i)(authorization|api[-_]?key|x-api-key)\s*[:=]\s*(?!<redacted>)\S", blob):
        return False
    if re.search(r"\bhttps?://[^\s\"']+/", blob):
        return False
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="runs/samples/minimax_connectivity_20260919_summary.json")
    parser.add_argument("--attempts", type=int, default=None)
    args = parser.parse_args()
    result = run(args.output, args.attempts)
    print(json.dumps({k: v for k, v in result.items() if k != "requests"}, ensure_ascii=True, indent=2))
    print(json.dumps(result["requests"], ensure_ascii=True, indent=2))
    raise SystemExit(0 if result["gate"] == "passed" else 1)
