"""tools/validate_mol.py - SMILES validity + Lipinski + SA score.

Input:  a single SMILES string, or a list of SMILES.
Output: a standardized JSON dict (or list of dicts).

Note: SA score requires sascorer.py (not in pip).
If the file is missing, the sa_score field returns None but other fields still work.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

from rdkit import Chem, RDLogger
from rdkit.Chem import Crippen, Descriptors, Lipinski, rdMolDescriptors

# Silence RDKit stderr warnings (invalid SMILES spams a lot)
RDLogger.DisableLog("rdApp.*")

# Lazy-loaded SA scorer
_sascorer = None
_sascorer_loaded = False


def _load_sascorer():
    """Lazy-load sascorer.py if present."""
    global _sascorer, _sascorer_loaded
    if _sascorer_loaded:
        return _sascorer
    _sascorer_loaded = True
    candidate = Path(__file__).parent / "sascorer.py"
    if candidate.exists():
        import importlib.util

        spec = importlib.util.spec_from_file_location("sascorer", candidate)
        if spec and spec.loader:
            _sascorer = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(_sascorer)
    return _sascorer


def compute_sa_score(mol: Chem.Mol) -> float | None:
    """Compute SA score (1 = easy to synthesize, 10 = nearly impossible)."""
    scorer = _load_sascorer()
    if scorer is None:
        return None
    try:
        return float(scorer.calculateScore(mol))
    except Exception:
        return None


# ----------------- Core API -----------------

def _stereochemistry_block(mol: Chem.Mol) -> dict:
    """Phase 4.4 (2026-09-27, P3-3): atom-level stereochemistry summary.

    Returns:
        n_stereocenters                total number of tetrahedral stereo
                                        centres (atoms with 4 different
                                        neighbours that RDKit recognises
                                        as chirality-relevant).
        n_unspecified_stereocenters    stereocenters whose ``@``/``@@``
                                        parity is NOT encoded in the input
                                        SMILES. The actionable signal:
                                        drug discovery wants this to be 0
                                        because racemic mixtures can have
                                        wildly different ADMET from a pure
                                        enantiomer.
        n_specified                    n_stereocenters - n_unspecified.
        has_double_bond_geometry       True if any double bond carries a
                                        ``/`` or ``\\`` annotation. (The
                                        actual E/Z counts are not surfaced
                                        here; that's a future enhancement.)
        canonical_with_stereo          canonical SMILES that PRESERVES the
                                        ``@``/``@@`` annotation. May differ
                                        from the regular canonical form when
                                        input omitted them.
        chirality                      one of {"chiral", "achiral",
                                        "racemic_mix", "unknown"}.

    Notes
    -----
    * Uses the stable RDKit counters
      ``CalcNumAtomStereoCenters`` / ``CalcNumUnspecifiedAtomStereoCenters``
      which have been available since RDKit 2020.09. We deliberately do NOT
      call ``Chem.AssignStereochemistryFrom3D`` (no 3D here) or
      ``Chem.AssignStereochemistry`` (only meaningful after 2D->3D embed).
      This function only reports what was already encoded in the input.
    * ``n_unspecified_stereocenters`` is the actionable signal: it counts
      atoms where the user (or the generator LLM) failed to annotate
      chirality on a stereogenic centre. Drug discovery wants this to be 0.
    """
    n_total = int(rdMolDescriptors.CalcNumAtomStereoCenters(mol))
    n_unspecified = int(rdMolDescriptors.CalcNumUnspecifiedAtomStereoCenters(mol))
    n_specified = max(0, n_total - n_unspecified)
    # Detect E/Z geometry by scanning bond directions. Cheap and
    # version-independent.
    has_directional = False
    for bond in mol.GetBonds():
        bt = bond.GetBondType()
        if bt == Chem.BondType.DOUBLE and (bond.GetStereo() != Chem.BondStereo.STEREONONE):
            has_directional = True
            break
    canonical_with_stereo = Chem.MolToSmiles(mol, isomericSmiles=True)
    canonical_without_stereo = Chem.MolToSmiles(mol, isomericSmiles=False)
    if n_total == 0 and not has_directional:
        chirality = "achiral"
    elif n_unspecified > 0:
        # Some chiral centres lack @/@@ annotation -> treat as racemic.
        chirality = "racemic_mix"
    elif canonical_with_stereo == canonical_without_stereo:
        # No stereo encoded in the SMILES at all (rare: would only happen
        # if every chiral centre was forced to be "ignored" by the parser).
        chirality = "achiral"
    else:
        chirality = "chiral"
    return {
        "n_stereocenters": n_total,
        "n_specified": n_specified,
        "n_unspecified_stereocenters": n_unspecified,
        "has_double_bond_geometry": bool(has_directional),
        "canonical_with_stereo": canonical_with_stereo,
        "chirality": chirality,
    }


def validate_smiles(smiles: str) -> dict:
    """Validate one SMILES and compute chemistry descriptors.

    Returns a dict with: valid, smiles (canonical), mw, logp, hbd, hba,
    rotatable_bonds, tpsa, rings, lipinski_pass, sa_score, and (Phase 4.4)
    a stereochemistry block (``n_stereocenters``, ``chirality``, etc.).
    """
    if not smiles or not isinstance(smiles, str):
        return {"valid": False, "smiles": str(smiles), "error": "empty or non-string input"}

    mol = Chem.MolFromSmiles(smiles.strip())
    if mol is None:
        return {"valid": False, "smiles": smiles, "error": "RDKit cannot parse SMILES"}

    props = {
        "valid": True,
        "smiles": Chem.MolToSmiles(mol),  # canonical (isomeric by default)
        "mw": round(Descriptors.MolWt(mol), 2),
        "logp": round(Crippen.MolLogP(mol), 2),
        "hbd": Lipinski.NumHDonors(mol),
        "hba": Lipinski.NumHAcceptors(mol),
        "rotatable_bonds": Lipinski.NumRotatableBonds(mol),
        "tpsa": round(Descriptors.TPSA(mol), 2),
        "rings": Descriptors.RingCount(mol),
    }
    props["lipinski_pass"] = lipinski_pass(props)
    props["sa_score"] = compute_sa_score(mol)
    # Phase 4.4 (P3-3): stereochemistry awareness.
    # Merge so n_stereocenters / chirality are first-class props.
    stereo = _stereochemistry_block(mol)
    props.update(stereo)
    return props


def lipinski_pass(props: dict, max_mw: float = 500, max_logp: float = 5,
                  max_hbd: int = 5, max_hba: int = 10) -> bool:
    """Lipinski rule-of-five (first four rules)."""
    if not props.get("valid"):
        return False
    return (
        props.get("mw", 0) <= max_mw
        and props.get("logp", 0) <= max_logp
        and props.get("hbd", 0) <= max_hbd
        and props.get("hba", 0) <= max_hba
    )


def validate_batch(smiles_list: Iterable[str]) -> list[dict]:
    """Validate a list of SMILES, returning results in input order."""
    return [validate_smiles(s) for s in smiles_list]


# ----------------- CLI -----------------

def _main():
    import json
    import sys

    if len(sys.argv) > 1:
        inputs = sys.argv[1:]
    else:
        inputs = ["CC(=O)Oc1ccccc1C(=O)O", "CCO", "InvalidSMILES"]

    results = validate_batch(inputs)
    print(json.dumps(results, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    _main()