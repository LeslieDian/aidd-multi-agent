"""agents/judge.py - Agent C (LLM critic).

Reads one round's results and suggests a SPECIFIC actionable focus for
the next round. Uses MiniMax-M3 with thinking enabled for best reasoning.

Phase 3 upgrade: instead of generic "keep exploring quinazoline", the
judge now names:
- A specific structural weakness to address
- A concrete modification (atom, group, scaffold)
- An expected property change (logP, MW, h-bond count)
"""
from __future__ import annotations

import json
import re

from .llm import get_client


SYSTEM_PROMPT = """You are an experienced medicinal chemist leading an
AI-driven EGFR inhibitor discovery project. You review each round of
generated candidates and write a SPECIFIC, ACTIONABLE focus instruction
for the next round.

Key EGFR facts to anchor your reasoning:
- ATP pocket has a hinge region with Met793 backbone NH (h-bond acceptor)
- Hydrophobic pocket lined by Leu694, Val702, Ala719, Leu820
- DFG motif at the activation loop
- Reference drugs: erlotinib (-7 to -8 kcal/mol), gefitinib, afatinib
- Strong inhibitors usually score <-7 kcal/mol on Vina with 22 A box

Your focus instruction MUST name:
1. A SPECIFIC structural element to change (e.g. "morpholine", "aniline NH", "quinazoline N1")
2. A specific replacement or addition (e.g. "replace with piperazine", "add N-H donor")
3. The expected property impact (e.g. "should drop logP by ~1.0", "add one h-bond to hinge")

Bad (too vague): "Try more diverse structures"
Bad (too vague): "Continue exploring quinazoline cores"
Good: "Replace the morpholine on candidate [0] with a piperazine bearing an N-methyl, should drop logP by ~0.8 and improve hinge engagement"
Good: "All candidates lack a hydrogen-bond donor to Met793; add a secondary amine on the western aryl ring"

Output STRICT JSON only:
{{
  "focus": "ONE specific 1-2 sentence instruction (max 250 chars)",
  "best_index": 0,
  "weakness": "the structural weakness you identified (1 sentence)",
  "expected_change": "what property should improve (e.g. 'logP -0.5')",
  "reasoning": "why this matters for EGFR binding (1 sentence)"
}}
"""


def judge_round(
    enriched: list[dict],
    config: dict,
    round_num: int,
    use_mock: bool = False,
) -> dict:
    """Judge one round's results, return focus for next round.

    Returns dict with keys: focus, best_index, reasoning, summary.
    On failure, returns a generic fallback focus.
    """
    # Judge provider: probe with a tiny test request, fall back if it fails
    from .llm import get_client as _gc
    judge_pref = config.get("llm", {}).get("judge", "deepseek")
    provider_name = None
    for probe in [judge_pref, "deepseek", "MiniMax"]:
        try:
            client = _gc(probe, config, mock=use_mock)
            # Make a tiny test call to verify auth works
            client.chat("hi", "ping", max_tokens=5)
            provider_name = probe
            break
        except Exception:
            continue
    if provider_name is None:
        provider_name = "deepseek"  # last resort, will likely fail and trigger fallback focus

    # Build compact input: top 5 candidates
    valid = [c for c in enriched if c["validate"]["valid"]]
    ranked = sorted(valid, key=lambda c: -c["composite_score"])[:5]

    summary_lines = [
        f"Round {round_num}: {len(enriched)} candidates, {len(valid)} valid."
    ]
    for i, c in enumerate(ranked):
        v = c["validate"]
        a = c["admet"]
        d = c["dock"]
        warnings = a.get("warnings", [])
        warn_str = "; ".join(warnings) if warnings else "none"
        summary_lines.append(
            f"[{i}] provider={c.get('provider', '?')} | "
            f"smiles={c['smiles']}\n"
            f"    MW={v.get('mw', '?')} logP={v.get('logp', '?')} "
            f"SA={v.get('sa_score', '?')} Lipinski={v.get('lipinski_pass', '?')} | "
            f"ADMET={a.get('summary_score', '?')} (QED={a.get('qed', '?')}) | "
            f"Vina={d.get('score', '?')} | composite={c.get('composite_score', '?')}\n"
            f"    warnings: {warn_str}"
        )
    user_prompt = (
        "Review this EGFR inhibitor round and identify ONE structural "
        "weakness shared by the candidates, then write the next-round "
        "focus:\n\n" + "\n".join(summary_lines)
    )

    fallback = {
        "focus": "Replace the western aryl ring with a smaller heterocycle to improve Vina binding.",
        "best_index": 0,
        "weakness": "(fallback: judge unavailable)",
        "expected_change": "logP -0.5",
        "reasoning": "Fallback focus used (LLM judge error).",
        "summary": {"error": "judge fallback"},
    }

    try:
        client = get_client(provider_name, config, mock=use_mock)
        raw = client.chat(SYSTEM_PROMPT, user_prompt, json_mode=True)
        parsed = _extract_json(raw)
        return {
            "focus": str(parsed.get("focus", "")).strip()[:300],
            "best_index": int(parsed.get("best_index", 0)),
            "weakness": str(parsed.get("weakness", "")).strip(),
            "expected_change": str(parsed.get("expected_change", "")).strip(),
            "reasoning": str(parsed.get("reasoning", "")).strip(),
            "summary": parsed,
        }
    except Exception as e:
        fallback["summary"] = {"error": f"{type(e).__name__}: {e}"}
        return fallback


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if m:
            text = m.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1:
        text = text[start : end + 1]
    return json.loads(text)