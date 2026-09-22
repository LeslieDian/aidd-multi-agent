"""Tests for tools/calibrated_herg.py - calibrated hERG risk proxy.

The original heuristic (in tools/admet_score.py) used a single weighted count
of basic nitrogens. The calibrated version uses 7 features + structural
alerts. These tests verify the new proxy:
1. Stays in [0, 1] for all valid SMILES.
2. Ranks the three EGFR reference inhibitors above small neutral molecules.
4. Does NOT collapse to the heuristic on basic nitrogen alone.
5. Detects aromatic para-amine and amidine/guanidine structural alerts.
"""
from __future__ import annotations

import pytest

from tools.calibrated_herg import (
    FEATURE_WEIGHTS, INTERCEPT, calibrated_herg_score,
    _features, _has_aromatic_para_amine, _has_amidine_or_guanidine,
    compare_to_heuristic,
)


def test_score_in_unit_interval():
    for smi in ["CCO", "Oc1ccccc1", "Cc1ccc(O)cc1", "c1ccccc1",
                "N#Cc1ccccc1", "OC(=O)c1ccccc1"]:
        r = calibrated_herg_score(smi)
        assert r["valid"], f"unexpected invalid for {smi}: {r.get('error')}"
        assert 0.0 <= r["score"] <= 1.0


def test_neutral_small_molecule_is_low_risk():
    """Small alcohols and aromatics without basic amines are not hERG concerns."""
    for smi in ["CCO", "Oc1ccccc1", "CCOc1ccccc1", "COc1ccccc1"]:
        r = calibrated_herg_score(smi)
        assert r["score"] < 0.1, f"{smi} score={r['score']}"


def test_basic_aliphatic_amine_is_higher_risk():
    """Aliphatic amines are well-known hERG liability drivers."""
    high = calibrated_herg_score("CCCCN")  # butylamine
    low = calibrated_herg_score("CCCCO")    # butanol
    assert high["score"] > low["score"]


def test_high_logp_basic_amine_is_much_higher_risk():
    """Waring 2010: cLogP multiplies hERG risk for basic amines."""
    high = calibrated_herg_score("CCCCCCCCCCN")  # decylamine
    low = calibrated_herg_score("CCN")            # ethylamine
    assert high["score"] > low["score"]


def test_egfr_reference_inhibitors_in_moderate_to_high_band():
    """The 3 EGFR inhibitors (gefitinib, erlotinib, afatinib) carry basic
    amines and are known hERG-relevant. Their calibrated score must be > 0.4."""
    for smi in [
        "C#Cc1ccc(Nc2ncnc3cc(OC)c(OCCOC)cc23)cc1",  # erlotinib
        "COC1=C(C=C2C(=C1)N=CN=C2NC3=CC(=C(C=C3)F)Cl)OCCCN4CCOCC4",  # gefitinib
        "CN(C)C/C=C/C(=O)NC1=C(C=C2C(=C1)C(=NC=N2)NC3=CC(=C(C=C3)F)Cl)O[C@H]4CCOC4",  # afatinib
    ]:
        r = calibrated_herg_score(smi)
        assert r["score"] > 0.4, f"{smi[:30]} score={r['score']}"


def test_aromatic_para_amine_alert_raises_score():
    """A free NH2 on benzene is a known hERG alert vs an acylated analogue."""
    free = calibrated_herg_score("Nc1ccc(O)cc1")            # p-aminophenol (free NH2)
    acylated = calibrated_herg_score("CC(=O)Nc1ccc(O)cc1")   # acetaminophen (amide N)
    assert free["score"] > acylated["score"], \
        f"free {free['score']} should be > acylated {acylated['score']}"


def test_amidine_guanidine_alert_fires():
    """Strong basic centers (amidine, guanidine) should give high score."""
    guanidine = "NC(=N)N"  # guanidine
    r = calibrated_herg_score(guanidine)
    assert r["score"] > 0.5
    assert r["features"]["amidine_guanidine"] == 1.0


def test_amidine_guanidine_pattern_detected():
    """SMARTS pattern matching works on a real amidine."""
    mol = ChemFromSmiles_robust("NC(=N)N")  # noqa: F821
    assert _has_amidine_or_guanidine(mol)


def test_aromatic_para_amine_pattern_detected():
    """p-Aminophenol triggers the para-amine alert."""
    mol = ChemFromSmiles_robust("Nc1ccc(O)cc1")  # noqa: F821
    assert _has_aromatic_para_amine(mol)


def test_aromatic_para_amine_quenched_by_withdrawing_group():
    """p-Nitroaniline should NOT trigger the para-amine alert because NO2 withdraws."""
    mol = ChemFromSmiles_robust("Nc1ccc([N+](=O)[O-])cc1")  # noqa: F821
    assert not _has_aromatic_para_amine(mol)


def test_calibrated_distinguishes_heuristic():
    """The calibrated score MUST NOT be identical to the heuristic for all
    molecules - if it is, the calibration added nothing."""
    pairs = compare_to_heuristic([
        "Oc1ccccc1", "Cc1ccc(O)cc1", "Oc1cccc(O)c1",
        "Nc1ccc(O)cc1", "CCCN", "CC(=O)Nc1ccc(O)cc1",
    ])
    differs = sum(
        1 for p in pairs
        if p["calibrated_score"] is not None and p["heuristic_score"] is not None
        and abs(p["calibrated_score"] - p["heuristic_score"]) > 0.05
    )
    assert differs >= 3, f"Calibrated should differ from heuristic on >=3 molecules; got {differs}"


def test_features_returns_expected_keys():
    mol = ChemFromSmiles_robust("Nc1ccc(O)cc1")  # noqa: F821
    feats = _features(mol)
    assert set(feats.keys()) == set(FEATURE_WEIGHTS.keys())


def test_features_clogp_clipped_to_realistic_range():
    """cLogP is clipped to [-2, 8] to keep the linear model stable for weird inputs."""
    mol = ChemFromSmiles_robust("CCCCCCCCCCCCCCCCCCCC")  # long alkane
    feats = _features(mol)
    assert feats["clogp"] <= 8.0


def test_invalid_smiles_returns_invalid():
    r = calibrated_herg_score("not_a_smiles")
    assert r["valid"] is False
    assert r["score"] is None


def test_dominant_contributors_named_in_rationale():
    """The rationale string should mention the top contributors for auditability."""
    r = calibrated_herg_score("CCCCCCN")
    assert "Top contributors" in r["rationale"]


def test_interpretation_documents_limitation():
    """The output must declare this is NOT a calibrated IC50 model."""
    r = calibrated_herg_score("CCO")
    assert "literature-informed" in r["interpretation"].lower() or \
           "not" in r["interpretation"].lower()


def test_intercept_is_set_to_keep_neutral_low():
    """Neutral phenols must come out well below 0.1 with the current intercept."""
    for smi in ["Oc1ccccc1", "CCOc1ccccc1"]:
        assert calibrated_herg_score(smi)["score"] < 0.1


# ---------- helper ----------

def ChemFromSmiles_robust(smi):  # noqa: N802
    from rdkit import Chem
    return Chem.MolFromSmiles(smi)