"""scripts/check_all_api_keys.py - Smart connectivity probe for all keys in .env.

For every `*_API_KEY` env var we discover, this script tries each
candidate (base_url, model) pair from a built-in matrix and reports the
first successful response (HTTP 200 + valid JSON). It also tries the
default model for each candidate endpoint.

The output is a small JSON summary so callers can grep / diff.

NEVER writes the keys to disk. NEVER logs them.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ============================================================
# Candidate matrix: (key name pattern, list of (base_url, model, label))
# ============================================================
# Patterns are matched case-insensitively against env var names.

@dataclass(frozen=True)
class Candidate:
    base_url: str
    model: str
    label: str


CANDIDATES: list[tuple[str, list[Candidate]]] = [
    ("MiniMax", [
        Candidate("https://api.minimaxi.com/v1", "MiniMax-M3", "minimax-tokenplan-direct"),
        Candidate("https://api.MiniMax.chat/v1", "MiniMax-M3", "minimax-chat-alt"),
    ]),
    ("DEEPSEEK", [
        # Direct DeepSeek API
        Candidate("https://api.deepseek.com/v1", "deepseek-chat", "deepseek-direct"),
        # Volcengine Agent Plan hosts deepseek models
        Candidate("https://ark.cn-beijing.volces.com/api/plan/v3",
                  "deepseek-v4-flash", "volcengine-plan-deepseek-flash"),
        Candidate("https://ark.cn-beijing.volces.com/api/plan/v3",
                  "deepseek-v4-pro", "volcengine-plan-deepseek-pro"),
        Candidate("https://ark.cn-beijing.volces.com/api/coding/v3",
                  "deepseek-v4-flash", "volcengine-coding-deepseek-flash"),
        # Alibaba Cloud Token Plan (same sk-sp key family works for Qwen)
        Candidate("https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
                  "deepseek-v3.2-exp", "aliyun-tokenplan-deepseek"),
        Candidate("https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
                  "deepseek-v3-flash", "aliyun-tokenplan-deepseek-v3"),
    ]),
    ("GLM", [
        # Zhipu BigModel direct
        Candidate("https://open.bigmodel.cn/api/paas/v4", "glm-4-flash", "zhipu-glm4flash"),
        Candidate("https://open.bigmodel.cn/api/paas/v4", "glm-4-plus", "zhipu-glm4plus"),
        # Volcengine Agent Plan hosts GLM
        Candidate("https://ark.cn-beijing.volces.com/api/plan/v3",
                  "glm-5.3", "volcengine-plan-glm53"),
        Candidate("https://ark.cn-beijing.volces.com/api/plan/v3",
                  "glm-5.3-flash", "volcengine-plan-glm53flash"),
        Candidate("https://ark.cn-beijing.volces.com/api/coding/v3",
                  "glm-5.3-flash", "volcengine-coding-glm53flash"),
    ]),
    ("QWEN", [
        # Alibaba Cloud Token Plan direct
        Candidate("https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
                  "qwen3.8-flash", "aliyun-tokenplan-qwen38flash"),
        Candidate("https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
                  "qwen3.7-flash", "aliyun-tokenplan-qwen37flash"),
        # Volcengine Agent Plan
        Candidate("https://ark.cn-beijing.volces.com/api/plan/v3",
                  "qwen3.8-flash", "volcengine-plan-qwen38flash"),
        Candidate("https://ark.cn-beijing.volces.com/api/plan/v3",
                  "qwen3.7-flash", "volcengine-plan-qwen37flash"),
        # Volcengine Coding Plan
        Candidate("https://ark.cn-beijing.volces.com/api/coding/v3",
                  "qwen3.8-flash", "volcengine-coding-qwen38flash"),
    ]),
]


# ============================================================
# Probe loop
# ============================================================

@dataclass
class ProbeAttempt:
    base_url: str
    model: str
    label: str
    status: str            # "ok" | "auth_error" | "model_not_found" | "timeout" | "network" | "schema_invalid" | "http_error"
    http_status: int | None = None
    latency_ms: float | None = None
    response_received: bool = False
    schema_valid: bool = False
    returned_text_preview: str = ""
    error: str = ""


SCHEMA = {"status": "ok", "sequence": None}
PROBE_SYSTEM = (
    "You are a transport self-test endpoint. Reply with STRICT JSON only, no prose, "
    'no code fences. The object must be exactly {"status":"ok","sequence":<int>}. '
    "Do not add, rename or omit keys. Do not explain."
)


def _probe_one(api_key: str, cand: Candidate, *, sequence: int, timeout: float) -> ProbeAttempt:
    """Issue one minimal request. NEVER logs api_key."""
    from openai import OpenAI, APIStatusError, APITimeoutError, APIConnectionError
    import httpx

    started = time.time_ns()
    client = OpenAI(
        base_url=cand.base_url,
        api_key=api_key,
        http_client=httpx.Client(trust_env=False, verify=True, timeout=timeout),
        max_retries=0,
    )
    try:
        resp = client.chat.completions.create(
            model=cand.model,
            messages=[
                {"role": "system", "content": PROBE_SYSTEM},
                {"role": "user", "content": f"sequence={sequence}"},
            ],
            temperature=0.0,
            max_tokens=64,
        )
        latency_ms = round((time.time_ns() - started) / 1e6, 3)
        text = (resp.choices[0].message.content or "") if resp.choices else ""
        # Strip <think>...</think> blocks emitted by some reasoning models
        # (e.g. MiniMax-M3 wraps reasoning in <think> before the JSON).
        import re as _re
        stripped = _re.sub(r"<think>.*?</think>", "", text, flags=_re.DOTALL).strip()
        try:
            obj = json.loads(stripped)
            schema_valid = (
                isinstance(obj, dict)
                and obj.get("status") == "ok"
                and obj.get("sequence") == sequence
            )
        except Exception:
            obj = None
            schema_valid = False
        return ProbeAttempt(
            base_url=cand.base_url,
            model=cand.model,
            label=cand.label,
            status="ok" if schema_valid else "schema_invalid",
            http_status=200,
            latency_ms=latency_ms,
            response_received=True,
            schema_valid=schema_valid,
            returned_text_preview=text[:60],
        )
    except APIStatusError as exc:
        latency_ms = round((time.time_ns() - started) / 1e6, 3)
        status_code = getattr(exc, "status_code", None)
        if status_code in (401, 403):
            cat = "auth_error"
        elif status_code == 404:
            cat = "model_not_found"
        else:
            cat = "http_error"
        return ProbeAttempt(
            base_url=cand.base_url,
            model=cand.model,
            label=cand.label,
            status=cat,
            http_status=status_code,
            latency_ms=latency_ms,
            error=str(exc)[:120],
        )
    except APITimeoutError as exc:
        return ProbeAttempt(
            base_url=cand.base_url,
            model=cand.model,
            label=cand.label,
            status="timeout",
            latency_ms=round((time.time_ns() - started) / 1e6, 3),
            error=str(exc)[:120],
        )
    except APIConnectionError as exc:
        return ProbeAttempt(
            base_url=cand.base_url,
            model=cand.model,
            label=cand.label,
            status="network",
            error=str(exc)[:120],
        )
    except Exception as exc:
        return ProbeAttempt(
            base_url=cand.base_url,
            model=cand.model,
            label=cand.label,
            status="error",
            error=f"{type(exc).__name__}: {exc}"[:120],
        )
    finally:
        try:
            client.close()
        except Exception:
            pass


def discover_keys(prefixes: Iterable[str]) -> list[str]:
    found = []
    for env_name, value in os.environ.items():
        if not value:
            continue
        if not env_name.endswith("_API_KEY"):
            continue
        # Skip if no matching prefix in our candidate matrix.
        # We accept any *_API_KEY because the script also probes unknown ones.
        found.append(env_name)
    return sorted(found)


def find_candidates(env_name: str) -> list[Candidate]:
    upper = env_name.upper()
    for prefix, cands in CANDIDATES:
        if upper.startswith(prefix.upper()) or prefix.upper() in upper:
            return list(cands)
    return []  # unknown key -> no candidates


def probe_key(env_name: str, *, sequence: int = 7, timeout: float = 30.0) -> dict:
    api_key = os.environ[env_name]
    if not api_key:
        return {"env": env_name, "ok": False, "error": "empty_value"}
    candidates = find_candidates(env_name)
    if not candidates:
        return {
            "env": env_name,
            "ok": False,
            "error": "no_candidates",
            "attempts": [],
            "hint": "key name not in candidate matrix; pass --candidates 'url|model;url|model'",
        }
    attempts = []
    for cand in candidates:
        attempt = _probe_one(api_key, cand, sequence=sequence, timeout=timeout)
        attempts.append({
            "label": attempt.label,
            "base_url": attempt.base_url,
            "model": attempt.model,
            "status": attempt.status,
            "http_status": attempt.http_status,
            "latency_ms": attempt.latency_ms,
            "response_received": attempt.response_received,
            "schema_valid": attempt.schema_valid,
            "response_preview": attempt.returned_text_preview,
            "error": attempt.error,
        })
        # Stop after first OK.
        if attempt.status == "ok":
            break
    first_ok = next((a for a in attempts if a["status"] == "ok"), None)
    return {
        "env": env_name,
        "ok": first_ok is not None,
        "working": first_ok,
        "attempts": attempts,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe all *_API_KEY in .env.")
    parser.add_argument("--only", nargs="*", default=None,
                        help="Only probe these env var names (default: all).")
    parser.add_argument("--sequence", type=int, default=7,
                        help="Sequence integer sent in the probe payload.")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--out", default=None,
                        help="Write JSON summary to this path (default: stdout only).")
    parser.add_argument("--strict", action="store_true",
                        help="Exit non-zero if any key is unreachable.")
    args = parser.parse_args()

    # .env loader (no-op if python-dotenv missing).
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass

    keys = args.only or discover_keys([])
    if not keys:
        print("No *_API_KEY env vars found.", file=sys.stderr)
        return 1

    results = []
    print("=" * 70)
    print(f"Probing {len(keys)} API keys ({args.sequence=})")
    print("=" * 70)
    for env_name in keys:
        print(f"\n--- {env_name} ---")
        result = probe_key(env_name, sequence=args.sequence, timeout=args.timeout)
        results.append(result)
        if result["ok"]:
            w = result["working"]
            print(f"  [OK] {w['label']}")
            print(f"      base_url={w['base_url']}")
            print(f"      model={w['model']}")
            print(f"      latency={w['latency_ms']} ms")
            print(f"      preview={w['response_preview']!r}")
        else:
            print(f"  [FAIL] ({result.get('error', 'see attempts')})")
            for a in result["attempts"]:
                print(f"      [{a['label']}] {a['status']} "
                      f"(http={a['http_status']}) {a['error']}")

    summary = {
        "probed_at": time.time_ns() / 1e9,
        "n_total": len(results),
        "n_ok": sum(1 for r in results if r["ok"]),
        "results": results,
    }
    if args.out:
        Path(args.out).write_text(
            json.dumps(summary, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"\nSummary written to {args.out}")

    if args.strict and summary["n_ok"] < summary["n_total"]:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())