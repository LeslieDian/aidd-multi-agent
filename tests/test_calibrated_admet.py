"""Tests for tools/calibrated_admet.py - calibrated multi-endpoint ADMET proxies.

Extends the calibrated-hERG pattern (7-feature logistic + transparency) to the
whole ADMET block. These tests verify:
1. Every endpoint score stays in [0, 1] for valid SMILES.
2. Endpoint directional sanity against known chemistry:
   - a large lipophilic aromatic amine scores higher on CYP inhibition proxy
     and lower on solubility than a small polar alcohol;
   - a small lipophilic molecule scores higher on BBB than a large polar one;
   - Veber-rule violators (many rotatable bonds / high TPSA) score lower on
     bioavailability;
   - the calibrated block is integrated into tools.admet_score.estimate_admet.
"""
from __future__ import annotations

from tools.calibrated_admet import (
    ENDPOINTS, calibrated_admet_scores, _features, batch,
)
from tools.admet_score import estimate_admet

ENDPOINT_NAMES = {"absorption", "bioavailability", "bbb_penetration",
                  "cyp_inhibition", "solubility", "metabolic_stability"}


def test_all_endpoints_in_unit_interval():
    for smi in ["CCO", "Oc1ccccc1", "c1ccccc1", "CCCCCCCCCCCCCCCC",
                "N#Cc1ccccc1", "OC(=O)c1ccccc1", "C1CCCCC1", "CCN(CC)C"]:
        r = calibrated_admet_scores(smi)
        assert r["valid"], f"unexpected invalid for {smi}: {r.get('error')}"
        assert set(r["endpoints"]) == ENDPOINT_NAMES
        for name, ep in r["endpoints"].items():
            assert 0.0 <= ep["score"] <= 1.0, f"{smi} {name}={ep['score']}"
        assert 0.0 <= r["admet_calibrated_summary"] <= 1.0


def test_endpoint_specs_have_weights_and_intercepts():
    assert set(ENDPOINTS) == ENDPOINT_NAMES
    for name, spec in ENDPOINTS.items():
        assert isinstance(spec["intercept"], (int, float))
        assert spec["weights"], f"{name} has no features"


def test_lipophilic_aromatic_amine_is_cyp_liability_and_less_soluble():
    big = calibrated_admet_scores("CCCCCCCCc1ccc2ccccc2c1N")   # lipophilic aromatic amine
    small = calibrated_admet_scores("CCO")                     # ethanol
    assert big["endpoints"]["cyp_inhibition"]["score"] > small["endpoints"]["cyp_inhibition"]["score"]
    assert small["endpoints"]["solubility"]["score"] > big["endpoints"]["solubility"]["score"]


def test_small_lipophilic_is_more_bbb_penetrant_than_large_polar():
    small = calibrated_admet_scores("CCCCCCCC")                # octane
    large_polar = calibrated_admet_scores("OC(CO)(CO)CO")       # pentaerythritol, big + polar
    assert small["endpoints"]["bbb_penetration"]["score"] > large_polar["endpoints"]["bbb_penetration"]["score"]


def test_veber_violator_scores_lower_bioavailability():
    # Flexible + polar: violates Veber (RotB > 10 and/or TPSA > 140).
    flexible = calibrated_admet_scores("CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC")
    rigid = calibrated_admet_scores("O=C1CC2CCC1C2")
    assert rigid["endpoints"]["bioavailability"]["score"] > flexible["endpoints"]["bioavailability"]["score"]


def test_integrated_into_estimate_admet():
    r = estimate_admet("CCO")
    assert r["valid"]
    assert r["calibrated_admet_summary"] is not None
    assert set(r["calibrated_admet_endpoints"]) == ENDPOINT_NAMES
    # hERG calibrated block still present (regression guard).
    assert "calibrated_herg_score" in r


def test_features_are_stable_keys():
    for smi in ["CCO", "c1ccccc1", "CCN(CC)C"]:
        mol_features = _features(_mol(smi))
        assert {"clogp", "mw", "tpsa", "rotatable_bonds", "hbd", "hba",
                "aromatic_rings", "heavy_atoms", "basic_nitrogen_count",
                "fraction_aromatic"} <= set(mol_features)


def _mol(smiles):
    from rdkit import Chem
    return Chem.MolFromSmiles(smiles)


def test_batch_returns_list():
    out = batch(["CCO", "c1ccccc1"])
    assert len(out) == 2
    assert all(r["valid"] for r in out)


def test_invalid_input_is_graceful():
    r = calibrated_admet_scores("not-a-smiles")
    assert r["valid"] is False
    assert r["admet_calibrated_summary"] is None
    assert r["endpoints"] == {}
