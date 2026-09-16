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
from .evaluator import candidate_priority_key
from tools.diversity import adoption_stats


SYSTEM_PROMPT = """You are an experienced medicinal chemist leading an
AI-driven EGFR inhibitor discovery project. You review each round of
generated candidates and write a SPECIFIC, ACTIONABLE focus instruction
for the next round.

Key EGFR facts to anchor your reasoning:
- ATP pocket has a hinge backbone NH (hydrogen-bond donor; verify residue numbering against receptor)
- Hydrophobic pocket lined by Leu694, Val702, Ala719, Leu820
- DFG motif at the activation loop
- Reference drugs: erlotinib, gefitinib; afatinib is a covalent inhibitor.
- Docking scores depend on protocol and do not establish measured affinity.

Your focus instruction MUST name:
1. A SPECIFIC structural element to change (e.g. "morpholine", "aniline NH", "quinazoline N1")
2. A specific replacement or addition (e.g. "replace with piperazine", "add N-H donor")
3. The expected property impact (e.g. "should drop logP by ~1.0", "add one h-bond to hinge")

Bad (too vague): "Try more diverse structures"
Bad (too vague): "Continue exploring quinazoline cores"
Good: "Replace the morpholine on candidate [0] with a piperazine bearing an N-methyl, should drop logP by ~0.8 and improve hinge engagement"
Good: "All candidates lack a hydrogen-bond donor to Met793; add a secondary amine on the western aryl ring"

SELF-REFLECTION (Phase 4.2):
When a "Previous focus" and "Previous candidates summary" are provided,
you MUST also reflect on whether your prior suggestion worked:
- Did the new molecules actually incorporate the prior focus? (e.g. did they add the morpholine you suggested?)
- Did Vina / ADMET improve, worsen, or stay flat?
- If the suggestion failed, do you pivot (different angle) or double down (refine wording)?
- DO NOT just repeat your previous focus uncritically.

MULTI-OBJECTIVE RULES:
- Prefer candidates on the Pareto front that also pass the safety gate.
- Never recommend a Vina improvement that knowingly raises the hERG-risk proxy.
- If binding and safety conflict, propose a structural change expected to improve
  the weaker objective while preserving the stronger one.

Output STRICT JSON only:
{{
  "focus": "ONE specific 1-2 sentence instruction (max 250 chars)",
  "best_index": 0,
  "weakness": "the structural weakness you identified (1 sentence)",
  "expected_change": "what property should improve (e.g. 'logP -0.5')",
  "reasoning": "why this matters for EGFR binding (1 sentence)",
  "reflection": "ONE sentence evaluating the previous round's outcome (max 200 chars; empty string if round 0)",
  "confidence": 0.0-1.0,
  "adopted_count": 0-N   // how many of the candidates this round visibly adopted the previous focus
}}
"""


def judge_round(
    enriched: list[dict],
    config: dict,
    round_num: int,
    previous_focus: str = "",
    previous_summary: dict | None = None,
    previous_enriched: list[dict] | None = None,
    use_mock: bool = False,
) -> dict:
    """Judge one round's results, return focus for next round.

    Phase 4.2: now also returns reflection + confidence + adopted_count.
    Phase 4.3 (P1-4 fix): if previous_enriched is provided, include every
    prior SMILES (with scaffold/MW/logP/Vina/ADMET/provider) in the
    reflection prompt. The Judge can then verify whether molecules this
    round actually adopted last round's focus, not just hand-wave
    "vina went down so my suggestion worked".

    Returns dict with keys: focus, best_index, weakness, expected_change,
    reasoning, reflection, confidence, adopted_count, summary.
    On failure, returns an explicit error and no new strategy.
    """
    provider_name = config.get("llm", {}).get("judge", "deepseek")

    # Build compact input: top 5 candidates
    valid = [c for c in enriched if c["validate"]["valid"]]
    complete = [c for c in valid if c.get("evaluation_status") == "complete"]
    ranked = sorted(complete or valid, key=candidate_priority_key, reverse=True)[:5]

    summary_lines = [
        f"Round {round_num}: {len(enriched)} candidates, {len(valid)} valid, "
        f"{len(complete)} fully docked."
    ]
    for i, c in enumerate(ranked):
        v = c["validate"]
        a = c["admet"]
        d = c["dock"]
        warnings = a.get("warnings", [])
        warn_str = "; ".join(warnings) if warnings else "none"
        summary_lines.append(
            f"[{i}] provider={c.get('provider', '?')} status={c.get('evaluation_status', 'unknown')} | "
            f"smiles={c['smiles']}\n"
            f"    MW={v.get('mw', '?')} logP={v.get('logp', '?')} "
            f"SA={v.get('sa_score', '?')} Lipinski={v.get('lipinski_pass', '?')} | "
            f"ADMET={a.get('summary_score', '?')} (QED={a.get('qed', '?')}, "
            f"hERG-risk={a.get('herg_risk_score', a.get('herg_risk', '?'))}) | "
            f"Vina={d.get('score', '?')} | composite={c.get('composite_score', '?')} | "
            f"Pareto={c.get('pareto_rank', '?')} safety={c.get('safety_gate_pass', '?')}\n"
            f"    warnings: {warn_str}"
        )

    user_prompt = (
        "Review this EGFR inhibitor round and identify ONE structural "
        "weakness shared by the candidates, then write the next-round focus.\n\n"
        + "\n".join(summary_lines)
        + "\nMissing scores are unknown, not success. ADMET values are descriptor heuristics. "
          "Prioritize safety-passing Pareto candidates. Do not trade a lower Vina score for higher hERG risk. "
          "Do not assert binding contacts without pose analysis. Structural suggestions are hypotheses. "
          "adopted_count covers only the displayed candidates and is an LLM estimate."
    )

    # Phase 4.2 + 4.3 (P1-4): inject previous round context for self-reflection.
    # If previous_enriched is provided we render every prior candidate with
    # SMILES + scaffold + MW + logP + ADMET + Vina + provider so the Judge can
    # truly verify whether the prior focus was adopted. Falls back to the
    # compressed previous_summary top_candidates when no enriched list is
    # available (e.g. round-0 startup or tests).
    if previous_focus:
        prev_vina = (previous_summary or {}).get("best_vina")
        prev_avg = (previous_summary or {}).get("avg_admet")
        prev_best = (previous_summary or {}).get("best_smiles")

        prev_mol_lines = []
        if previous_enriched:
            for i, c in enumerate(previous_enriched):
                if not c.get("validate", {}).get("valid"):
                    continue
                v = c.get("validate", {}) or {}
                a = c.get("admet", {}) or {}
                d = c.get("dock", {}) or {}
                prev_mol_lines.append(
                    f"  [{i}] prov={c.get('provider', '?')} "
                    f"smi={c.get('smiles')} "
                    f"scaffold={c.get('scaffold', '?')} | "
                    f"MW={v.get('mw', '?')} logP={v.get('logp', '?')} "
                    f"ADMET={a.get('summary_score', '?')} "
                    f"Vina={d.get('score', '?')}"
                )
        elif previous_summary:
            # Fallback: compressed top-3 with smiles + score only
            for i, tc in enumerate((previous_summary or {}).get("top_candidates", [])):
                prev_mol_lines.append(
                    f"  [{i}] smi={tc.get('smiles')} score={tc.get('score')} "
                    f"Vina={tc.get('vina')}"
                )

        reflection_section = (
            f"\n\nPREVIOUS ROUND CONTEXT (for self-reflection):\n"
            f"- Previous focus: {previous_focus}\n"
            f"- Previous best Vina: {prev_vina}\n"
            f"- Previous best SMILES: {prev_best}\n"
            f"- Previous avg ADMET: {prev_avg}\n"
            f"- Previous candidates ({len(prev_mol_lines)} molecules):\n"
            + ("\n".join(prev_mol_lines) if prev_mol_lines else "  (no candidate detail available)")
            + "\n\n"
            f"Reflect: did the new molecules adopt your previous focus? "
            f"Did Vina / ADMET improve? Should you pivot or double down?\n"
            f"Fill 'reflection', 'confidence', and 'adopted_count' accordingly."
        )
        user_prompt = user_prompt + reflection_section

    fallback = {
        "focus": "",
        "status": "error",
        "provider": provider_name,
        "is_mock": use_mock,
        "best_index": 0,
        "weakness": "(fallback: judge unavailable)",
        "expected_change": "",
        "reasoning": "Fallback focus used (LLM judge error).",
        "reflection": "",
        "confidence": 0.0,
        "adopted_count": 0,
        "adoption_deterministic": {
            "n_total": 0, "n_valid_sim": 0, "n_adopted": 0,
            "adoption_rate": None, "max_similarity": None,
            "mean_similarity": None, "threshold": 0.7, "reference": None,
        },
        "adoption_llm_vs_det_drift": None,
        "summary": {"error": "judge fallback"},
    }

    client, raw = None, None
    try:
        client = get_client(provider_name, config, mock=use_mock)
        # Inject curated SAR facts (binding-mode + western aryl + tail SAR)
        # into the Judge system prompt so it can ground "weakness" in
        # EGFR-specific structural axes (hinge bidentate, pocket-I aryl,
        # C7 basic-amine tail) instead of generic "be more diverse".
        from tools.references import format_sar_for_prompt
        sar_block = format_sar_for_prompt()
        judge_system = SYSTEM_PROMPT + ("\n\n" + sar_block if sar_block else "")
        raw = client.chat(judge_system, user_prompt, json_mode=True)
        parsed = _extract_json(raw)

        # Round 0 (or any round without previous_focus): reflection must be
        # empty because there is nothing to reflect on. Don't trust LLM here.
        if previous_focus:
            try:
                conf = float(parsed.get("confidence", 0.5))
                conf = max(0.0, min(1.0, conf))
            except (TypeError, ValueError):
                conf = 0.5
            try:
                adopted = int(parsed.get("adopted_count", 0))
                adopted = min(len(ranked), max(0, adopted))
            except (TypeError, ValueError):
                adopted = 0
            reflection_text = str(parsed.get("reflection", "")).strip()[:300]
        else:
            conf, adopted, reflection_text = 0.0, 0, ""

        # Phase 4.3 (P1-3 fix): deterministic adoption check via Tanimoto
        # similarity to the previous round's best SMILES. This is the
        # ground-truth reference for the Judge's LLM-estimated `adopted_count`.
        # We always compute the structural similarity (cheap and useful as
        # a learning-curve metric) regardless of whether previous_focus exists.
        adoption_threshold = float(
            (config.get("judge", {}) or {}).get(
                "adoption_tanimoto_threshold",
                (config.get("llm", {}) or {}).get(
                    "adoption_tanimoto_threshold", 0.7
                ),
            )
        )
        ref_smiles = (previous_summary or {}).get("best_smiles")
        current_smiles = [c.get("smiles") for c in ranked if c.get("smiles")]
        det = adoption_stats(
            current_smiles, ref_smiles or "", threshold=adoption_threshold
        ) if ref_smiles else {
            "n_total": len(current_smiles),
            "n_valid_sim": 0,
            "n_adopted": 0,
            "adoption_rate": None,
            "max_similarity": None,
            "mean_similarity": None,
            "threshold": adoption_threshold,
            "reference": None,
        }
        # LLM-vs-deterministic drift: a large absolute gap flags that the
        # Judge is hallucinating adoption. Sign is informative (over/under).
        if previous_focus and ref_smiles:
            drift = adopted - det["n_adopted"]
        else:
            drift = None

        return {
            "status": "ok",
            "provider": provider_name,
            "model": client.model,
            "is_mock": use_mock,
            "usage": getattr(client, "last_usage", {}),
            "prompt": {"system": SYSTEM_PROMPT, "user": user_prompt},
            "raw_response": raw,
            "adoption_method": "llm_estimate_displayed_candidates",
            "adoption_denominator": len(ranked),
            "focus": str(parsed.get("focus", "")).strip()[:300],
            "best_index": min(max(0, int(parsed.get("best_index", 0))), len(ranked) - 1) if ranked else None,
            "displayed_candidate_ids": [c.get("candidate_id") for c in ranked],
            "weakness": str(parsed.get("weakness", "")).strip(),
            "expected_change": str(parsed.get("expected_change", "")).strip(),
            "reasoning": str(parsed.get("reasoning", "")).strip(),
            "reflection": reflection_text,
            "confidence": conf,
            "adopted_count": adopted,
            # Phase 4.3 (P1-3): structural reality check
            "adoption_deterministic": det,
            "adoption_llm_vs_det_drift": drift,
            "summary": parsed,
        }
    except Exception as e:
        fallback["summary"] = {"error": f"{type(e).__name__}: {e}"}
        fallback.update(prompt={"system": SYSTEM_PROMPT, "user": user_prompt},
                        raw_response=raw, usage=getattr(client, "last_usage", {}),
                        model=getattr(client, "model", None))
        return fallback


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
