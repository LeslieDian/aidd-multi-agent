"""Transparent RDKit descriptor heuristics used for early AIDD triage.

These values are screening signals. They are not measured ADMET endpoints or
calibrated toxicity probabilities. The continuous hERG risk score is an
explicit heuristic so multi-objective optimization can detect safety drift.
"""
from __future__ import annotations

import math
from typing import Iterable

from rdkit import Chem, RDLogger
from rdkit.Chem import Descriptors, Lipinski, QED, rdMolDescriptors

RDLogger.DisableLog("rdApp.*")


def _is_carbonyl_or_sulfonyl_neighbor(atom: Chem.Atom) -> bool:
    """Approximate whether a nitrogen is deactivated as amide/sulfonamide."""
    for neighbor in atom.GetNeighbors():
        if neighbor.GetAtomicNum() not in {6, 16}:
            continue
        for bond in neighbor.GetBonds():
            other = bond.GetOtherAtom(neighbor)
            if other.GetIdx() == atom.GetIdx():
                continue
            if other.GetAtomicNum() in {8, 16} and bond.GetBondTypeAsDouble() >= 1.9:
                return True
    return False


def _basic_nitrogen_profile(mol: Chem.Mol) -> tuple[int, float]:
    """Return count and weighted basicity proxy for nitrogen environments."""
    count = 0
    score = 0.0
    for atom in mol.GetAtoms():
        if atom.GetAtomicNum() != 7 or atom.GetFormalCharge() < 0:
            continue
        if _is_carbonyl_or_sulfonyl_neighbor(atom):
            continue
        if atom.GetIsAromatic() and atom.GetTotalNumHs() > 0:
            continue
        count += 1
        if atom.GetIsAromatic():
            # Pyridine/quinazoline-like nitrogens are much weaker bases than
            # aliphatic amines; keep a small contribution instead of treating
            # them as equivalent hERG drivers.
            score += 0.15
        elif any(neighbor.GetIsAromatic() for neighbor in atom.GetNeighbors()):
            # Aniline-like nitrogen is resonance-deactivated.
            score += 0.35
        else:
            score += 1.0
    return count, score


def _sigmoid(value: float) -> float:
    return 1.0 / (1.0 + math.exp(-value))


def estimate_admet(smiles: str) -> dict:
    """Estimate transparent descriptor-based ADMET and safety proxies."""
    if not smiles or not isinstance(smiles, str):
        return {"valid": False, "smiles": str(smiles), "error": "empty input"}

    mol = Chem.MolFromSmiles(smiles.strip())
    if mol is None:
        return {"valid": False, "smiles": smiles, "error": "Invalid SMILES"}

    tpsa = float(Descriptors.TPSA(mol))
    rot_bonds = int(Lipinski.NumRotatableBonds(mol))
    logp = float(Descriptors.MolLogP(mol))
    mw = float(Descriptors.MolWt(mol))
    qed = float(QED.qed(mol))
    aromatic_rings = int(rdMolDescriptors.CalcNumAromaticRings(mol))
    basic_nitrogens, basic_nitrogen_score = _basic_nitrogen_profile(mol)
    warnings: list[str] = []

    if tpsa <= 140:
        absorption = 1.0
    elif tpsa <= 180:
        absorption = 0.5
        warnings.append("TPSA>140: reduced passive-absorption proxy")
    else:
        absorption = 0.0
        warnings.append("TPSA>180: poor passive-absorption proxy")

    if rot_bonds <= 10 and tpsa <= 140:
        bioavailability = 1.0
    elif rot_bonds <= 15:
        bioavailability = 0.5
        warnings.append("Veber flexibility/polarity warning")
    else:
        bioavailability = 0.0
        warnings.append("rotatable_bonds>15")

    if basic_nitrogen_score > 0:
        lipophilic_term = _sigmoid((logp - 3.5) / 0.6)
        basic_term = min(1.0, basic_nitrogen_score)
        aromatic_term = min(1.0, aromatic_rings / 3.0)
        size_term = max(0.0, min(1.0, (mw - 350.0) / 250.0))
        herg_risk_score = min(
            1.0,
            basic_term * (0.75 * lipophilic_term + 0.15 * aromatic_term)
            + 0.10 * size_term,
        )
    else:
        herg_risk_score = 0.0
    herg_risk = 1.0 if herg_risk_score >= 0.5 else 0.0
    if herg_risk:
        warnings.append("hERG descriptor-risk proxy >= 0.5")

    admet_quality_score = 0.375 * absorption + 0.375 * bioavailability + 0.25 * qed
    safety_score = 1.0 - herg_risk_score
    summary_score = 0.8 * admet_quality_score + 0.2 * safety_score

    return {
        "valid": True,
        "smiles": Chem.MolToSmiles(mol),
        "method": "rdkit_descriptor_heuristic_v2",
        "is_trained_admet_model": False,
        "interpretation": (
            "Screening heuristics, not measured ADMET or calibrated toxicity probabilities"
        ),
        "absorption": absorption,
        "bioavailability": bioavailability,
        "herg_risk": herg_risk,
        "herg_risk_score": round(herg_risk_score, 6),
        "safety_score": round(safety_score, 6),
        "qed": round(qed, 6),
        "logp": round(logp, 6),
        "mw": round(mw, 6),
        "tpsa": round(tpsa, 6),
        "rotatable_bonds": rot_bonds,
        "aromatic_rings": aromatic_rings,
        "basic_nitrogen_count": basic_nitrogens,
        "basic_nitrogen_score": round(basic_nitrogen_score, 6),
        "admet_quality_score": round(admet_quality_score, 6),
        "summary_score": round(summary_score, 6),
        "warnings": warnings,
        # 2026-09-22 (P3): calibrated hERG risk proxy (7-feature logistic).
        # Independent of the single-feature `herg_risk_score` above; both are
        # reported so callers can compare. The calibrated version is what the
        # 4-category memory + dock-aware diagnostic read.
        **calibrated_herg_block(smiles),
    }


def calibrated_herg_block(smiles: str) -> dict:
    """Inline integration of tools.calibrated_herg.calibrated_herg_score.

    Returns only the keys that should land in the admet payload, so callers
    get a stable, documented surface.
    """
    try:
        from tools.calibrated_herg import calibrated_herg_score
        r = calibrated_herg_score(smiles)
        if not r.get("valid"):
            return {"calibrated_herg_score": None, "calibrated_herg_features": None}
        return {
            "calibrated_herg_score": r.get("score"),
            "calibrated_herg_features": r.get("features"),
            "calibrated_herg_dominant": r.get("dominant_contributors"),
        }
    except Exception as exc:  # pragma: no cover - integration shim
        return {"calibrated_herg_score": None, "calibrated_herg_features": None,
                "calibrated_herg_error": f"{type(exc).__name__}: {exc}"}


def admet_batch(smiles_list: Iterable[str]) -> list[dict]:
    return [estimate_admet(smiles) for smiles in smiles_list]


def _main() -> None:
    import json
    import sys

    inputs = sys.argv[1:] if len(sys.argv) > 1 else ["CCO", "CC(=O)Oc1ccccc1C(=O)O"]
    print(json.dumps(admet_batch(inputs), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    _main()
