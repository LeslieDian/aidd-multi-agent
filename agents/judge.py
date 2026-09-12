"""agents/judge.py - Agent C (LLM critic).

Reads one round's results and suggests focus for the next round.
Uses MiniMax-M3 with thinking enabled for best reasoning quality.
"""
from __future__ import annotations

import json
import re

from .llm import get_client


SYSTEM_PROMPT = """You are an experienced drug-discovery project lead.
You review each round of an AI-driven molecule-generation loop, looking
at the candidates, their scores, and the chemists' rationale.

Your job: write a SHORT focus instruction (1-2 sentences, max 200 chars)
that the next-round generator should follow to improve the candidates.

Common focus patterns:
- "Try replacing the X with Y to improve binding affinity"
- "Push MW higher (~450) for more kinase selectivity"
- "Add a polar group on the left side of the scaffold to engage hinge"
- "Explore more diverse cores (try pyrimidine instead of quinazoline)"

Output STRICT JSON only:
{{
  "focus": "your 1-2 sentence instruction here",
  "best_index": 0,
  "reasoning": "why you chose that candidate and focus"
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
    provider_name = config.get("llm", {}).get("judge", "judge_MiniMax")

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
        summary_lines.append(
            f"[{i}] smiles={c['smiles']} | provider={c.get('provider', '?')} | "
            f"MW={v.get('mw', '?')} logP={v.get('logp', '?')} "
            f"SA={v.get('sa_score', '?')} | "
            f"ADMET={a.get('summary_score', '?')} | "
            f"Vina={d.get('score', '?')} | "
            f"composite={c['composite_score']}"
        )
    user_prompt = "Review this round and write next-round focus:\n\n" + "\n".join(summary_lines)

    fallback = {
        "focus": "Continue exploring quinazoline cores with diverse substituents; aim for MW 350-450.",
        "best_index": 0,
        "reasoning": "Fallback focus used (judge unavailable).",
        "summary": {"error": "judge fallback"},
    }

    try:
        client = get_client(provider_name, config, mock=use_mock)
        raw = client.chat(SYSTEM_PROMPT, user_prompt, json_mode=True)
        parsed = _extract_json(raw)
        return {
            "focus": str(parsed.get("focus", "")).strip()[:300],
            "best_index": int(parsed.get("best_index", 0)),
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