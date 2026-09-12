"""agents/evaluator.py - Agent B (no LLM, calls Phase 1 tools).

For each candidate SMILES, runs all 4 scoring tools and returns a
merged record. Failures are isolated per molecule.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

from tools import (
    validate_batch,
    admet_batch,
    scaffold_diversity,
    dock_smiles,
    get_scaffold,
)


def evaluate_candidates(
    candidates: list[dict],
    scoring_config: dict,
    target_config: dict,
    dock_enabled: bool = True,
) -> list[dict]:
    """Evaluate a batch of candidates.

    Args:
        candidates: list of {"smiles": str, "provider": str, ...}
        scoring_config: config["scoring"] dict
        target_config: config["target"] dict
        dock_enabled: skip Vina if False (faster iteration in dev)

    Returns:
        list of enriched candidates with keys:
            smiles, provider, model, rationale,
            validate, admet, dock, scaffold,
            composite_score
    """
    smiles_list = [c["smiles"] for c in candidates]

    # ---- 1) validate_mol + admet (cheap, always run) ----
    val_results = validate_batch(smiles_list)
    admet_results = admet_batch(smiles_list)

    # ---- 2) dock (slow; run only for valid molecules if enabled) ----
    pocket = target_config["pocket"]
    receptor = target_config["receptor_pdbqt"]
    dock_results = []
    if dock_enabled:
        for smi, v in zip(smiles_list, val_results):
            if v.get("valid"):
                dock_results.append(dock_smiles(
                    smi, receptor,
                    (pocket["center_x"], pocket["center_y"], pocket["center_z"]),
                    (pocket["size_x"], pocket["size_y"], pocket["size_z"]),
                    exhaustiveness=scoring_config.get("vina", {}).get("exhaustiveness", 8),
                ))
            else:
                dock_results.append({"smiles": smi, "score": None, "valid": False,
                                     "error": "skipped (invalid SMILES)"})
    else:
        for smi in smiles_list:
            dock_results.append({"smiles": smi, "score": None, "valid": True,
                                 "error": "dock disabled"})

    # ---- 3) scaffolds ----
    scaffolds = [get_scaffold(s) for s in smiles_list]

    # ---- 4) Merge and compute composite ----
    enriched = []
    for cand, val, admet, dock, scaf in zip(
        candidates, val_results, admet_results, dock_results, scaffolds
    ):
        composite = _composite_score(val, admet, dock, scoring_config)
        enriched.append({
            **cand,
            "validate": val,
            "admet": admet,
            "dock": dock,
            "scaffold": scaf,
            "composite_score": composite,
        })
    return enriched


def _composite_score(val: dict, admet: dict, dock: dict, cfg: dict) -> float:
    """Weighted score combining the 4 tools. Returns 0 if molecule invalid."""
    if not val.get("valid"):
        return 0.0
    score = 0.0
    total_weight = 0.0

    # ADMET (weight 0.4)
    if admet.get("valid"):
        score += 0.4 * admet.get("summary_score", 0.0)
        total_weight += 0.4

    # Lipinski (weight 0.1)
    if val.get("lipinski_pass"):
        score += 0.1
        total_weight += 0.1

    # SA score (weight 0.2; reward low SA, penalty above threshold)
    sa = val.get("sa_score")
    if sa is not None:
        threshold = cfg.get("sa_score", {}).get("reject_above", 6.0)
        sa_component = max(0.0, 1.0 - (sa - 1.0) / 9.0)  # 1 -> 1.0, 10 -> 0.0
        if sa > threshold:
            sa_component *= 0.3  # penalty but not zero
        score += 0.2 * sa_component
        total_weight += 0.2

    # Vina docking (weight 0.3; more negative = better)
    vina = dock.get("score")
    if vina is not None:
        threshold = cfg.get("vina", {}).get("accept_below", -8.0)
        # normalize to [0, 1]: -12 -> 1.0, 0 -> 0.0
        vina_component = max(0.0, min(1.0, (-vina) / 12.0))
        if vina > threshold:
            vina_component *= 0.3
        score += 0.3 * vina_component
        total_weight += 0.3

    return round(score / max(total_weight, 1e-6), 3)


def summarize_round(enriched: list[dict]) -> dict:
    """Quick statistics for one round (used by loop.py)."""
    valid = [c for c in enriched if c["validate"]["valid"]]
    docked = [c for c in valid if c["dock"].get("valid") and c["dock"].get("score") is not None]
    scaffolds = {c["scaffold"] for c in valid if c["scaffold"]}

    return {
        "n_total": len(enriched),
        "n_valid": len(valid),
        "valid_ratio": round(len(valid) / max(len(enriched), 1), 3),
        "n_docked": len(docked),
        "n_unique_scaffolds": len(scaffolds),
        "avg_admet": round(sum(c["admet"]["summary_score"] for c in valid) / max(len(valid), 1), 3),
        "best_vina": min((c["dock"]["score"] for c in docked), default=None) if docked else None,
        "top_candidates": sorted(
            [{"smiles": c["smiles"], "score": c["composite_score"]} for c in valid],
            key=lambda x: -x["score"],
        )[:3],
    }