"""agents/prompts/ - Prompt template registry (Phase 4.6 multi-agent).

Each role-specific prompt template lives in its own file. Templates are
loaded by name through `agents.prompts.registry.load(role)` and rendered
against a small set of placeholders:

    {n}            - number of candidates requested
    {focus}        - current focus from prior round's judge
    {weakness}     - current weakness from prior round's judge
    {memory}       - working memory compressed context
    {parents_block}- Phase 4.5 selection-operator block
    {failed_prompt}- failed-ligand set prompt injection
    {target}       - target description (e.g. EGFR / 1M17)

The generator user-prompt template in `agents.generator` already supports
these placeholders for the *single* (default) prompt. Multi-agent adds
the `role` axis so different generators see different framings.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Mapping

log = logging.getLogger(__name__)

_HERE = Path(__file__).resolve().parent


# Built-in default templates. Templates are simple str.format-style strings.
# Adding a new role = adding a new entry + a corresponding .md file is optional.
DEFAULT_TEMPLATES: dict[str, str] = {
    "qed": (
        "You are an expert medicinal chemist focused on **drug-likeness and "
        "ADMET**. Generate {n} diverse SMILES that improve QED, logP, "
        "polar surface area, and hERG safety relative to the parent. "
        "Avoid known PAINS. Return only JSON.\n\n"
        "Focus this round on: {focus}\n"
        "Address this structural weakness: {weakness}\n"
        "Context from prior rounds: {memory}\n"
        "{parents_block}\n"
        "{failed_prompt}"
    ),
    "vina": (
        "You are an expert computational chemist focused on **binding affinity "
        "and docking**. Generate {n} SMILES predicted to bind tightly to the "
        "target pocket (lower Vina / better shape complementarity). You may "
        "sacrifice some QED for binding gains; do not violate hard safety. "
        "Return only JSON.\n\n"
        "Focus this round on: {focus}\n"
        "Address this structural weakness: {weakness}\n"
        "Context from prior rounds: {memory}\n"
        "{parents_block}\n"
        "{failed_prompt}"
    ),
    "synth": (
        "You are an expert process chemist focused on **synthetic accessibility "
        "and short reaction routes**. Generate {n} SMILES with low SA score, "
        "commercially available building blocks, and minimal protecting-group "
        "needs. Return only JSON.\n\n"
        "Focus this round on: {focus}\n"
        "Address this structural weakness: {weakness}\n"
        "Context from prior rounds: {memory}\n"
        "{parents_block}\n"
        "{failed_prompt}"
    ),
    "default": (
        "Generate {n} diverse SMILES for the target. {focus} {weakness} "
        "{memory} {parents_block} {failed_prompt} Return only JSON."
    ),
}

# Expert prompts activated by the router (Phase 4.6 stage 5).
# Defined AFTER DEFAULT_TEMPLATES so they can prefix into the role templates.
DEFAULT_TEMPLATES["prompt_qed_expert"]    = "PROPERTY EXPERT ACTIVATED. " + DEFAULT_TEMPLATES["qed"]
DEFAULT_TEMPLATES["prompt_vina_expert"]   = "DOCKING EXPERT ACTIVATED. " + DEFAULT_TEMPLATES["vina"]
DEFAULT_TEMPLATES["prompt_sa_expert"]     = "SA EXPERT ACTIVATED. " + DEFAULT_TEMPLATES["synth"]
DEFAULT_TEMPLATES["prompt_exploit_expert"] = (
    "EXPLOIT MODE: best-known candidate is strong. Generate {n} small "
    "structural perturbations. {focus} {weakness} {memory} {parents_block} "
    "{failed_prompt} Return only JSON."
)

# Judge prompts (Phase 4.7: 5-role contract closure).
# Multi-agent's three judges (J1_property / J2_docking / J3_synthesis)
# previously fell through to the role-specific generator template, which is
# wrong: a judge reviews candidates, it doesn't generate them. These judge
# templates give each judge a proper brief that asks for an actionable
# next-round focus instead of new SMILES.
DEFAULT_TEMPLATES["judge_property"] = (
    "You are an experienced medicinal chemist reviewing the latest round of "
    "EGFR candidate molecules. Your role is **property / ADMET critic**.\n\n"
    "Look at the candidates and prior focus in the conversation. Produce a "
    "STRICT JSON object with these fields and NOTHING else:\n"
    "{{"
    "\"focus\": \"ONE specific 1-2 sentence instruction for the next round "
    "(max 250 chars, naming the structural element to change and the expected "
    "property impact)\","
    "\"weakness\": \"the dominant property weakness you identified (1 sentence)\","
    "\"confidence\": 0.0-1.0,"
    "\"reflection\": \"ONE sentence evaluating the previous round's outcome "
    "(max 200 chars; empty string if round 0)\""
    "}}"
)
DEFAULT_TEMPLATES["judge_docking"] = (
    "You are an experienced computational chemist reviewing the latest round "
    "of EGFR candidate molecules. Your role is **binding / docking critic**.\n\n"
    "Look at the candidates and prior focus in the conversation. Produce a "
    "STRICT JSON object with these fields and NOTHING else:\n"
    "{{"
    "\"focus\": \"ONE specific 1-2 sentence instruction about scaffold / pose / "
    "hinge engagement for the next round (max 250 chars)\","
    "\"weakness\": \"the dominant binding-mode weakness you identified (1 sentence)\","
    "\"confidence\": 0.0-1.0,"
    "\"reflection\": \"ONE sentence evaluating the previous round's outcome "
    "(max 200 chars; empty string if round 0)\""
    "}}"
)
DEFAULT_TEMPLATES["judge_synthesis"] = (
    "You are an experienced process chemist reviewing the latest round of "
    "EGFR candidate molecules. Your role is **synthetic accessibility / "
    "commercial availability critic**.\n\n"
    "Look at the candidates and prior focus in the conversation. Produce a "
    "STRICT JSON object with these fields and NOTHING else:\n"
    "{{"
    "\"focus\": \"ONE specific 1-2 sentence instruction about synthetic "
    "feasibility / SA score / building blocks for the next round "
    "(max 250 chars)\","
    "\"weakness\": \"the dominant synthesis weakness you identified (1 sentence)\","
    "\"confidence\": 0.0-1.0,"
    "\"reflection\": \"ONE sentence evaluating the previous round's outcome "
    "(max 200 chars; empty string if round 0)\""
    "}}"
)


def load(role: str) -> str:
    """Return the prompt template for `role`. Falls back to 'default'."""
    if role in DEFAULT_TEMPLATES:
        return DEFAULT_TEMPLATES[role]
    # Try to load from a .md / .j2 file next to this module.
    for ext in (".md", ".j2", ".txt"):
        path = _HERE / f"{role}{ext}"
        if path.exists():
            return path.read_text(encoding="utf-8")
    return DEFAULT_TEMPLATES["default"]


def render(role: str, placeholders: Mapping[str, str]) -> str:
    """Render `role` template with given placeholders.

    Missing placeholders are rendered as empty strings (no KeyError).
    Falls back to a regex strip on unrecoverable format errors; that
    fallback is logged so silent content loss is at least visible in
    audit logs (Phase 4.7 fix for the silent-fallback bug noted in the
    earlier review).
    """
    template = load(role)
    import re as _re
    referenced = set(_re.findall(r"\{(\w+)\}", template))
    safe = {k: str(v) for k, v in placeholders.items()}
    for k in referenced:
        safe.setdefault(k, "")
    try:
        return template.format(**safe)
    except (KeyError, IndexError) as exc:
        # Last resort strip - but log it so we don't silently lose content.
        log.warning(
            "[prompts] render(role=%r) format() failed (%s); "
            "falling back to regex strip that may lose content.",
            role, exc,
        )
        return _re.sub(r"\{[^}]*\}", "", template)