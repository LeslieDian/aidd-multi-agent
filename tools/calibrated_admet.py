"""Calibrated multi-endpoint ADMET proxies.

Extends the calibrated-hERG pattern (tools/calibrated_herg.py) to the whole
ADMET block: each endpoint is a transparent, literature-informed logistic
score over explicit RDKit descriptors, so callers can see WHICH features
drive a molecule's risk instead of receiving an opaque number.

Endpoints (all in [0, 1]):
  absorption            higher = better oral passive absorption proxy
  bioavailability       higher = better oral bioavailability proxy (Veber)
  bbb_penetration       higher = more CNS-penetrant (risk for peripherally
                        acting drugs; desired for CNS targets)
  cyp_inhibition        higher = higher CYP3A4/2D6 inhibition-likelihood proxy
                        (risk: drug-drug interactions)
  solubility            higher = better aqueous solubility proxy (ESOL-style)
  metabolic_stability   higher = more metabolically stable proxy (lower
                        first-pass liability)

We do NOT claim these are fitted ADMET models. We DO claim they are more
discriminating than the single-criterion heuristic block in
tools/admet_score.py, and they share the calibrated-hERG transparency
contract: every endpoint returns its named features, weighted contributions,
and dominant contributors.
"""
from __future__ import annotations

import math
from typing import Iterable

from rdkit import Chem, RDLogger
from rdkit.Chem import Descriptors, Lipinski, rdMolDescriptors

RDLogger.DisableLog("rdApp.*")


# ---------- Feature extractors (shared) ----------


def _is_carbonyl_or_sulfonyl_neighbor(atom: Chem.Atom) -> bool:
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


def _basic_nitrogen_count(mol: Chem.Mol) -> int:
    """Non-deactivated (non-amide/sulfonamide, non-pyrrole) basic N count."""
    count = 0
    for atom in mol.GetAtoms():
        if atom.GetAtomicNum() != 7 or atom.GetFormalCharge() < 0:
            continue
        deactivated = False
        for nb in atom.GetNeighbors():
            if nb.GetAtomicNum() not in {6, 16}:
                continue
            for bond in nb.GetBonds():
                other = bond.GetOtherAtom(nb)
                if other.GetIdx() == atom.GetIdx():
                    continue
                if other.GetAtomicNum() in {8, 16} and bond.GetBondTypeAsDouble() >= 1.9:
                    deactivated = True
        if deactivated:
            continue
        if atom.GetIsAromatic() and atom.GetTotalNumHs() > 0:
            continue
        count += 1
    return count


def _features(mol: Chem.Mol) -> dict:
    """Shared descriptor feature set used by every endpoint."""
    clogp = float(Descriptors.MolLogP(mol))
    mw = float(Descriptors.MolWt(mol))
    psa = float(Descriptors.TPSA(mol))
    rot_bonds = int(Lipinski.NumRotatableBonds(mol))
    hbd = int(Lipinski.NumHDonors(mol))
    hba = int(Lipinski.NumHAcceptors(mol))
    aromatic_rings = int(rdMolDescriptors.CalcNumAromaticRings(mol))
    heavy_atoms = mol.GetNumHeavyAtoms()
    return {
        "clogp": max(-4.0, min(10.0, clogp)),
        "mw": max(100.0, min(1000.0, mw)),
        "tpsa": max(0.0, min(300.0, psa)),
        "rotatable_bonds": rot_bonds,
        "hbd": hbd,
        "hba": hba,
        "aromatic_rings": aromatic_rings,
        "heavy_atoms": heavy_atoms,
        "basic_nitrogen_count": _basic_nitrogen_count(mol),
        "fraction_aromatic": (sum(1 for a in mol.GetAtoms() if a.GetIsAromatic())
                              / heavy_atoms) if heavy_atoms else 0.0,
    }


def _sigmoid(value: float) -> float:
    return 1.0 / (1.0 + math.exp(-value))


# ---------- Endpoint specifications ----------
# Each endpoint: name -> {intercept, weights: feature -> weight, direction note}

ENDPOINTS = {
    "absorption": {
        "label": "Oral passive absorption proxy",
        "intercept": 2.2,
        # Higher TPSA beyond ~140 hurts; higher logP up to a point helps;
        # large MW hurts. Weights are log-odds contributions.
        "weights": {
            "tpsa": -0.02,          # beyond ~140, permeability drops
            "clogp": 0.10,          # lipophilic drives passive diffusion
            "mw": -0.004,           # >500 hurts (Lipinski)
            "hbd": -0.25,           # H-bond donors hurt permeability
            "hba": -0.06,           # H-bond acceptors hurt weakly
            "rotatable_bonds": -0.08,
        },
    },
    "bioavailability": {
        "label": "Oral bioavailability proxy (Veber)",
        "intercept": 3.0,
        "weights": {
            "rotatable_bonds": -0.18,   # Veber: >10 hurts
            "tpsa": -0.022,             # Veber: >140 hurts
            "mw": -0.003,               # large molecules hurt
            "hbd": -0.20,
        },
    },
    "bbb_penetration": {
        "label": "Blood-brain-barrier penetration proxy",
        "intercept": -2.5,
        "weights": {
            "clogp": 0.45,          # lipophilic penetrates better
            "mw": -0.004,           # >450 sharply reduces BBB
            "tpsa": -0.025,         # >90 sharply reduces BBB (central rule)
            "hbd": -0.30,           # HBD >3 sharply reduces BBB
            "basic_nitrogen_count": 0.25,  # basic amines aid CNS uptake
        },
    },
    "cyp_inhibition": {
        "label": "CYP3A4/2D6 inhibition-likelihood proxy",
        "intercept": -3.8,
        "weights": {
            "aromatic_rings": 0.35,        # lipophilic aromatics bind heme
            "basic_nitrogen_count": 0.60,  # 2D6 prefers basic amines
            "clogp": 0.18,                 # lipophilicity drives inhibition
            "mw": 0.002,
            "hba": 0.05,
        },
    },
    "solubility": {
        "label": "Aqueous solubility proxy (ESOL-flavoured)",
        "intercept": 3.4,
        # ESOL: logS = 0.16 - 0.63*clogP - 0.0062*MW + 0.066*RB - 0.74*AP.
        # We invert to a 0..1 "better solubility" score with negative logP
        # contributions.
        "weights": {
            "clogp": -0.85,            # lipophilicity hurts solubility
            "mw": -0.008,              # size hurts
            "rotatable_bonds": 0.06,   # flexibility helps (ESOL)
            "fraction_aromatic": -1.2, # aromatic bulk hurts
            "heavy_atoms": -0.02,
        },
    },
    "metabolic_stability": {
        "label": "Metabolic stability proxy (first-pass liability)",
        "intercept": 1.8,
        "weights": {
            "rotatable_bonds": -0.15,      # flexible sites are oxidized
            "aromatic_rings": -0.25,       # aromatic rings are CYP substrates
            "clogp": -0.10,                # lipophilicity predicts clearance
            "basic_nitrogen_count": -0.20, # basic amines are N-dealkylated
            "mw": -0.002,
        },
    },
}


# ---------- Public API ----------


def calibrated_admet_scores(smiles: str) -> dict:
    """Return calibrated multi-endpoint ADMET proxies for one molecule.

    Returns:
        {
          "valid": bool,
          "smiles": canonical,
          "endpoints": {name: {score, features, dominant_contributors, rationale}},
          "admet_calibrated_summary": float (mean of endpoint scores),
        }
    """
    if not smiles or not isinstance(smiles, str):
        return {"valid": False, "smiles": str(smiles), "error": "empty input",
                "endpoints": {}, "admet_calibrated_summary": None}
    mol = Chem.MolFromSmiles(smiles.strip())
    if mol is None:
        return {"valid": False, "smiles": smiles, "error": "invalid SMILES",
                "endpoints": {}, "admet_calibrated_summary": None}

    feats = _features(mol)
    endpoints = {}
    for name, spec in ENDPOINTS.items():
        log_odds = spec["intercept"]
        for feature, weight in spec["weights"].items():
            log_odds += weight * feats[feature]
        score = _sigmoid(log_odds)
        contributors = sorted(
            [(f, spec["weights"][f] * feats[f]) for f in spec["weights"] if feats[f] > 0],
            key=lambda x: x[1], reverse=True,
        )[:3]
        parts = [f"{f}={feats[f]:.2f}" for f, _ in contributors] or ["no positive features"]
        endpoints[name] = {
            "score": round(float(score), 6),
            "features": {f: round(float(feats[f]), 4) for f in spec["weights"]},
            "dominant_contributors": [{"feature": f, "weighted": round(v, 3)} for f, v in contributors],
            "rationale": "Top contributors: " + ", ".join(parts),
        }

    summary = round(float(sum(e["score"] for e in endpoints.values()) / len(endpoints)), 6)
    return {
        "valid": True,
        "smiles": Chem.MolToSmiles(mol),
        "method": "calibrated_multi_endpoint_admet_v1",
        "is_trained_admet_model": False,
        "interpretation": (
            "Literature-informed multi-endpoint ADMET proxies; NOT fitted models. "
            "Each endpoint is a transparent logistic over explicit RDKit "
            "descriptors; use for relative ranking within a chemical series."
        ),
        "endpoints": endpoints,
        "admet_calibrated_summary": summary,
    }


def batch(smiles_list: Iterable[str]) -> list[dict]:
    return [calibrated_admet_scores(smi) for smi in smiles_list]
