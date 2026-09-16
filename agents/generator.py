"""agents/generator.py - Agent A (heterogeneous SMILES generator).

Calls multiple LLM providers in parallel, each returning a JSON list of
candidate SMILES + design rationale. The JSON format is enforced via
OpenAI-compatible `response_format: {type: json_object}`.
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Iterable

from .llm import LLMClient, MockLLMClient

# ---------- Prompt templates ----------

SYSTEM_PROMPT = """You are a senior medicinal chemist designing drug-like \
molecules that bind the ATP-binding pocket of EGFR (PDB: 1M17), \
a tyrosine kinase implicated in non-small-cell lung cancer.

Known verified reference inhibitors:
__REFERENCES__

Design constraints (drug-likeness):
- Molecular weight 280-500 Da
- logP between 1.0 and 4.5
- H-bond donors <= 3, acceptors <= 8
- TPSA <= 110 A^2
- At least one aromatic ring (essential for hinge binding)
- At least one H-bond acceptor (N or O) that can reach the hinge backbone
- Avoid combining high lipophilicity with strongly basic amine tails; target logP <= 4.5
- Treat docking, drug-like properties, synthetic accessibility, and hERG-risk
  as separate objectives; propose trade-off candidates rather than optimizing
  docking alone

Output STRICT JSON only (no markdown, no commentary):
{{
  "smiles_list": ["SMILES1", "SMILES2", ...],
  "rationale": "One-sentence design rationale describing your strategy."
}}

Rules:
- Provide exactly __N__ SMILES.
- Every SMILES must be valid (RDKit-parseable).
- Vary scaffolds across the list to maximize diversity.
- Never include commentary outside the JSON object.
"""


USER_PROMPT_TEMPLATE = """Generate __N__ new candidate EGFR inhibitor SMILES.
__FOCUS__

__WEAKNESS__

__MEMORY__

__FAILED__
Return ONLY the JSON object.
"""


# ---------- Validation ----------

def _extract_json(text: str) -> dict:
    """Return the first complete JSON object, ignoring surrounding text."""
    text = text.strip()
    decoder = json.JSONDecoder()
    last_error: Exception | None = None
    for match in re.finditer(r"\{", text):
        try:
            parsed, _ = decoder.raw_decode(text[match.start():])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError as exc:
            last_error = exc
    if last_error:
        raise last_error
    raise ValueError("No JSON object in response")


# ---------- Single-provider call ----------

def generate_with_provider(
    provider_name: str,
    config: dict,
    n: int = 5,
    focus: str = "",
    weakness: str = "",
    memory_context: str = "",
    failed_prompt: str = "",
    use_mock: bool = False,
) -> dict:
    """Call one LLM provider once, return parsed JSON.

    Phase 4.1: memory_context (WorkingMemory) and failed_prompt (FailedLigandSet)
    are injected into the user prompt so the generator has context from prior rounds.

    Returns: {"model": str, "provider": str, "smiles_list": [...], "rationale": str}
    Raises on parsing failure.
    """
    from .llm import get_client

    client = get_client(provider_name, config, mock=use_mock)
    focus_section = f"Focus this round on: {focus}" if focus else ""
    weakness_section = f"Address this structural weakness: {weakness}" if weakness else ""
    memory_section = f"Context from prior rounds: {memory_context}" if memory_context else ""
    user = (
        USER_PROMPT_TEMPLATE
        .replace("__N__", str(n))
        .replace("__FOCUS__", focus_section)
        .replace("__WEAKNESS__", weakness_section)
        .replace("__MEMORY__", memory_section)
        .replace("__FAILED__", failed_prompt)
    )
    from tools.references import load_references, format_sar_for_prompt
    references = "\n".join(f"- {name}: {row['smiles']} (PubChem CID {row['CID']})"
                           for name, row in load_references().items())
    sar_block = format_sar_for_prompt()
    references_block = references + ("\n\n" + sar_block if sar_block else "")
    system = SYSTEM_PROMPT.replace("__N__", str(n)).replace("__REFERENCES__", references_block)

    raw = client.chat(system=system, user=user, json_mode=True)
    result = {
        "model": getattr(client, "model", "unknown"), "provider": provider_name,
        "raw_length": len(raw), "is_mock": use_mock,
        "usage": getattr(client, "last_usage", {}),
        "prompt": {"system": system, "user": user}, "raw_response": raw,
        "smiles_list": [], "rationale": "",
    }
    try:
        parsed = _extract_json(raw)
        smiles_list = parsed.get("smiles_list", [])
        if not isinstance(smiles_list, list) or not smiles_list:
            raise ValueError("No smiles_list in response")
        result.update(smiles_list=[str(s).strip() for s in smiles_list][:n],
                      rationale=str(parsed.get("rationale", "")).strip())
    except Exception as exc:
        result['error'] = f'{type(exc).__name__}: {exc}'
    return result


# ---------- Parallel multi-provider call ----------

def generate_candidates(
    config: dict,
    providers: Iterable[str] | None = None,
    n_per_provider: int = 5,
    focus: str = "",
    weakness: str = "",
    memory_context: str = "",
    failed_prompt: str = "",
    use_mock: bool = False,
    max_workers: int = 4,
    max_attempts_per_provider: int = 1,
) -> list[dict]:
    """Generate candidates from multiple providers in parallel.

    Phase 4.1: memory_context and failed_prompt are passed to every provider.

    Returns one dict per provider call. Failed calls are returned with
    `error` field instead of raising, so the loop can continue.
    """
    providers = list(providers or config.get("llm", {}).get("generators", []))
    if not providers:
        raise ValueError("no providers configured")

    if max_attempts_per_provider < 1:
        raise ValueError("max_attempts_per_provider must be positive")

    def generate_with_retries(provider: str) -> dict:
        errors: list[str] = []
        result: dict = {}
        for attempt in range(1, max_attempts_per_provider + 1):
            try:
                result = generate_with_provider(
                    provider, config, n_per_provider, focus, weakness,
                    memory_context, failed_prompt, use_mock,
                )
            except Exception as exc:
                result = {
                    "provider": provider,
                    "smiles_list": [],
                    "rationale": "",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            actual = len(result.get("smiles_list") or [])
            if not result.get("error") and actual == n_per_provider:
                result["attempt_count"] = attempt
                if errors:
                    result["prior_attempt_errors"] = errors
                return result
            errors.append(
                str(result.get("error") or
                    f"candidate_count={actual} expected={n_per_provider}")
            )
        result["attempt_count"] = max_attempts_per_provider
        result["attempt_errors"] = errors
        if not result.get("error"):
            result["error"] = errors[-1]
        return result

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=min(max_workers, len(providers))) as ex:
        futures = {
            ex.submit(generate_with_retries, p): p
            for p in providers
        }
        for fut in as_completed(futures):
            provider = futures[fut]
            try:
                results.append(fut.result())
            except Exception as e:
                results.append({
                    "provider": provider,
                    "error": f"{type(e).__name__}: {e}",
                    "smiles_list": [],
                    "rationale": "",
                })
    return results
