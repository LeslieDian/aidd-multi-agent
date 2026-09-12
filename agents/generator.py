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

Known reference inhibitors (use these as inspiration, NOT as direct copies):
- Erlotinib: C#Cc1ccc(Nc2ncnc3cc(OCCOC)c(OCCOC)cc23)cc1
- Gefitinib: COc1cc2ncnc(Nc3ccc(F)c(Cl)c3)c2cc1OCCCN1CCOCC1
- Afatinib: CN(C)C(=O)C1=CC=CC=C1C(=O)Nc1ncnc2cc(NCc3ccc(C)cc3)c(OC)cc12

Design constraints (drug-likeness):
- Molecular weight 280-500 Da
- logP between 1.0 and 4.5
- H-bond donors <= 3, acceptors <= 8
- TPSA <= 110 A^2
- At least one aromatic ring (essential for hinge binding)
- At least one H-bond acceptor (N or O) that can reach the hinge backbone

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

Return ONLY the JSON object.
"""


# ---------- Validation ----------

def _extract_json(text: str) -> dict:
    """Robustly extract JSON from LLM output (handle code fences etc.)."""
    text = text.strip()
    # strip ```json ... ```
    if text.startswith("```"):
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if m:
            text = m.group(1)
    # find first { ... last }
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start : end + 1]
    return json.loads(text)


# ---------- Single-provider call ----------

def generate_with_provider(
    provider_name: str,
    config: dict,
    n: int = 5,
    focus: str = "",
    use_mock: bool = False,
) -> dict:
    """Call one LLM provider once, return parsed JSON.

    Returns: {"model": str, "provider": str, "smiles_list": [...], "rationale": str}
    Raises on parsing failure.
    """
    from .llm import get_client

    client = get_client(provider_name, config, mock=use_mock)
    focus_section = f"Focus this round on: {focus}" if focus else ""
    user = USER_PROMPT_TEMPLATE.replace("__N__", str(n)).replace("__FOCUS__", focus_section)
    system = SYSTEM_PROMPT.replace("__N__", str(n))

    raw = client.chat(system=system, user=user, json_mode=True)
    parsed = _extract_json(raw)

    smiles_list = parsed.get("smiles_list", [])
    if not isinstance(smiles_list, list) or not smiles_list:
        raise ValueError(f"{provider_name}: no smiles_list in response")

    return {
        "model": getattr(client, "model", "unknown"),
        "provider": provider_name,
        "smiles_list": [str(s).strip() for s in smiles_list][:n],
        "rationale": str(parsed.get("rationale", "")).strip(),
        "raw_length": len(raw),
    }


# ---------- Parallel multi-provider call ----------

def generate_candidates(
    config: dict,
    providers: Iterable[str] | None = None,
    n_per_provider: int = 5,
    focus: str = "",
    use_mock: bool = False,
    max_workers: int = 4,
) -> list[dict]:
    """Generate candidates from multiple providers in parallel.

    Returns one dict per provider call. Failed calls are returned with
    `error` field instead of raising, so the loop can continue.
    """
    providers = list(providers or config.get("llm", {}).get("generators", []))
    if not providers:
        raise ValueError("no providers configured")

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=min(max_workers, len(providers))) as ex:
        futures = {
            ex.submit(
                generate_with_provider,
                p, config, n_per_provider, focus, use_mock,
            ): p
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