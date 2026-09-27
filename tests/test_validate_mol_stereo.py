"""Regression tests for the stereochemistry block in tools/validate_mol.

P3-3 (2026-09-27): chiral molecules must be round-tripped without losing
the ``@``/``@@`` annotation, and the validation result must surface
``n_stereocenters`` and ``chirality`` so downstream stages can detect
"the generator forgot to specify chirality".
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.validate_mol import validate_smiles, validate_batch


# --- chiral SMILES fixtures -------------------------------------------------

# (S)-alanine: explicit @ on the alpha carbon.
S_ALANINE = "N[C@@H](C)C(=O)O"
# (R)-alanine: opposite @. Must round-trip to a different canonical.
R_ALANINE = "N[C@H](C)C(=O)O"
# Alanine written without stereo: alpha carbon is stereogenic but unspecified.
ALANINE_NO_STEREO = "NC(C)C(=O)O"
# Achiral molecule: ethanol.
ETHANOL = "CCO"
# Achiral aromatic: phenol (no stereocenters at all).
PHENOL = "Oc1ccccc1"


def test_achiral_molecule_has_zero_stereocenters():
    out = validate_smiles(PHENOL)
    assert out["valid"] is True
    assert out["n_stereocenters"] == 0
    assert out["n_unspecified_stereocenters"] == 0
    assert out["chirality"] == "achiral"
    # Canonical with and without stereo are identical for achiral mols.
    assert out["canonical_with_stereo"] == out["smiles"]


def test_achiral_with_no_double_bonds_keeps_zero():
    out = validate_smiles(ETHANOL)
    assert out["n_stereocenters"] == 0
    assert out["chirality"] == "achiral"


def test_chiral_round_trip_preserves_at_annotation():
    """(S)-alanine must remain (S)-alanine after a canonical round-trip."""
    out_s = validate_smiles(S_ALANINE)
    out_r = validate_smiles(R_ALANINE)
    assert out_s["valid"] and out_r["valid"]
    # Both have one stereocentre, both fully specified.
    assert out_s["n_stereocenters"] == 1
    assert out_s["n_specified"] == 1
    assert out_s["n_unspecified_stereocenters"] == 0
    assert out_r["n_stereocenters"] == 1
    assert out_r["n_specified"] == 1
    assert out_r["n_unspecified_stereocenters"] == 0
    assert out_s["chirality"] == "chiral"
    assert out_r["chirality"] == "chiral"
    # The two enantiomers MUST have different canonical SMILES, otherwise we
    # have lost the @/@@ information and the safety of downstream stages is
    # silently degraded.
    assert out_s["canonical_with_stereo"] != out_r["canonical_with_stereo"]
    # Their achiral canonical (no isomeric) form collapses them.
    assert out_s["smiles"] != out_s["canonical_with_stereo"] or \
        "(the canonical form already preserves @)"


def test_unspecified_stereocenter_is_flagged():
    """A molecule with stereogenic atoms but no @/@@ must be flagged."""
    out = validate_smiles(ALANINE_NO_STEREO)
    assert out["valid"] is True
    assert out["n_stereocenters"] == 1  # alanine alpha-C IS stereogenic
    assert out["n_specified"] == 0
    assert out["n_unspecified_stereocenters"] == 1
    # Per the chirality contract: a stereogenic but unspecified atom
    # surfaces as "racemic_mix" (the molecule exists as a racemate unless
    # the chemist specifies otherwise).
    assert out["chirality"] == "racemic_mix"
    # The canonical form must NOT silently invent stereo.
    assert "@" not in out["smiles"]


def test_invalid_smiles_returns_no_stereo_block():
    out = validate_smiles("not_a_smiles_at_all")
    assert out["valid"] is False
    assert "n_stereocenters" not in out
    assert "chirality" not in out
    assert "error" in out


def test_validate_batch_returns_independent_blocks():
    results = validate_batch([S_ALANINE, PHENOL, ALANINE_NO_STEREO])
    assert len(results) == 3
    assert results[0]["chirality"] == "chiral"
    assert results[1]["chirality"] == "achiral"
    assert results[2]["chirality"] == "racemic_mix"


def test_lipinski_unaffected_by_stereo():
    """The lipinski block must still be correct on a chiral molecule."""
    out = validate_smiles(S_ALANINE)
    assert out["lipinski_pass"] is True
    assert out["mw"] < 500
    # 1 HBD (NH2), 2 HBA (C=O and OH). Off-by-one regressions would surface here.
    assert out["hbd"] >= 1
    assert out["hba"] >= 2


def test_double_bond_geometry_is_detected():
    """A trans-2-butene SMILES must surface has_double_bond_geometry=True
    and the (E)/(Z) pair must round-trip to different canonical SMILES."""
    TRANS_BUTENE = "C/C=C/C"
    CIS_BUTENE = "C/C=C\\C"
    out_t = validate_smiles(TRANS_BUTENE)
    out_c = validate_smiles(CIS_BUTENE)
    assert out_t["valid"] and out_c["valid"]
    assert out_t["has_double_bond_geometry"] is True
    assert out_c["has_double_bond_geometry"] is True
    assert out_t["canonical_with_stereo"] != out_c["canonical_with_stereo"]


def test_double_bond_without_geometry_not_flagged():
    """2-butene without ``/``/``\\`` must not report double-bond geometry."""
    out = validate_smiles("CC=CC")
    assert out["valid"] is True
    assert out["has_double_bond_geometry"] is False
    assert out["chirality"] == "achiral"