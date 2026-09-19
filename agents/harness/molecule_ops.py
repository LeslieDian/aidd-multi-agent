"""Deterministic molecule import, refinement verification and goal checks."""
from __future__ import annotations

from copy import deepcopy
import math

from rdkit import Chem, DataStructs
from rdkit.Chem import rdFMCS, rdFingerprintGenerator
from rdkit.Chem.Scaffolds import MurckoScaffold


DEFAULT_CONSTRAINTS = {
    "require_option_screening": False,
    "require_planned_edits": False,
    "max_edits": 3,
    "preserve_scaffold": True,
    "protected_smarts": [],
    "allowed_parent_atom_indices": [],
    "max_heavy_atom_delta": 6,
    "max_changed_atoms": 8,
    "min_similarity": 0.25,
    "require_verified_refinement": False,
    "require_safety_gate": False,
    "min_property_score": None,
    "min_composite_score": None,
    "max_vina_score": None,
    "allow_generation": True,
    "allow_refine": True,
    "allow_freeform_refine": True,
    "docking_allowed": False,
    "require_meaningful_improvement": False,
    "min_effects": {"property_score": 0.01, "composite_score": 0.01, "vina": 0.5, "herg_risk": 0.02},
    "max_regressions": {"property_score": 0.01, "herg_risk": 0.0},
}


def normalize_constraints(value: dict | None, *, dock_enabled: bool | None = None) -> dict:
    """Return a complete, JSON-safe constraint set and reject ambiguous input."""
    raw = {} if value is None else value
    if not isinstance(raw, dict):
        raise ValueError("constraints must be an object")
    unknown = set(raw) - set(DEFAULT_CONSTRAINTS)
    if unknown:
        raise ValueError("Unknown constraints: " + ", ".join(sorted(unknown)))
    result = deepcopy(DEFAULT_CONSTRAINTS)
    result.update(raw)
    if dock_enabled is not None and "docking_allowed" not in raw:
        result["docking_allowed"] = bool(dock_enabled)
    for key in ("preserve_scaffold", "require_verified_refinement", "require_safety_gate",
                "allow_generation", "allow_refine", "allow_freeform_refine", "docking_allowed",
                "require_meaningful_improvement", "require_planned_edits", "require_option_screening"):
        if type(result[key]) is not bool:
            raise ValueError(f"{key} must be boolean")
    protected = result["protected_smarts"]
    if (not isinstance(protected, list) or len(protected) > 20
            or any(not isinstance(item, str) or not item.strip() for item in protected)):
        raise ValueError("protected_smarts must contain at most 20 nonempty SMARTS strings")
    for smarts in protected:
        if Chem.MolFromSmarts(smarts) is None:
            raise ValueError(f"Invalid protected SMARTS: {smarts}")
    allowed = result["allowed_parent_atom_indices"]
    if (not isinstance(allowed, list) or len(allowed) > 100
            or any(type(index) is not int or index < 0 for index in allowed)
            or len(set(allowed)) != len(allowed)):
        raise ValueError("allowed_parent_atom_indices must contain unique nonnegative integers")
    for key, upper in (("max_heavy_atom_delta", 100), ("max_changed_atoms", 200), ("max_edits", 100)):
        if type(result[key]) is not int or not 0 <= result[key] <= upper:
            raise ValueError(f"{key} must be an integer between 0 and {upper}")
    if not isinstance(result["min_similarity"], (int, float)) or isinstance(result["min_similarity"], bool):
        raise ValueError("min_similarity must be numeric")
    result["min_similarity"] = float(result["min_similarity"])
    if not 0 <= result["min_similarity"] <= 1:
        raise ValueError("min_similarity must be between 0 and 1")
    for key in ("min_property_score", "min_composite_score", "max_vina_score"):
        number = result[key]
        if number is not None:
            if not isinstance(number, (int, float)) or isinstance(number, bool) or not math.isfinite(number):
                raise ValueError(f"{key} must be null or a finite number")
            result[key] = float(number)
    for key in ("min_property_score", "min_composite_score"):
        if result[key] is not None and not 0 <= result[key] <= 1:
            raise ValueError(f"{key} must be between 0 and 1")
    metrics = set(DEFAULT_CONSTRAINTS["min_effects"])
    for key in ("min_effects", "max_regressions"):
        limits = result[key]
        if not isinstance(limits, dict) or set(limits) - metrics:
            raise ValueError(f"{key} must map known metrics to numerical limits")
        if key == "min_effects" and set(limits) != metrics:
            raise ValueError("min_effects must specify all four metrics")
        for metric, value in limits.items():
            if (type(value) not in (int, float) or not math.isfinite(value)
                    or value < 0 or (key == "min_effects" and value == 0)):
                raise ValueError(f"{key}.{metric} must be finite and " + ("positive" if key == "min_effects" else "nonnegative"))
    return result


def canonicalize(smiles: str) -> str:
    if not isinstance(smiles, str) or not smiles.strip():
        raise ValueError("SMILES must be a nonempty string")
    mol = Chem.MolFromSmiles(smiles.strip())
    if mol is None:
        raise ValueError(f"Invalid SMILES: {smiles}")
    return Chem.MolToSmiles(mol)


def add_seed_candidates(state, smiles_list: list[str], source: str = "user") -> list[str]:
    if not isinstance(smiles_list, list) or not 1 <= len(smiles_list) <= 20:
        raise ValueError("seed_smiles must contain 1-20 SMILES strings")
    existing = {candidate["smiles"] for candidate in state.candidates.values()}
    added = []
    for raw in smiles_list:
        smiles = canonicalize(raw)
        if smiles in existing:
            continue
        candidate_id = f"c{len(state.candidates) + 1}"
        state.candidates[candidate_id] = {
            "candidate_id": candidate_id,
            "smiles": smiles,
            "parent_id": None,
            "candidate_role": "seed",
            "source": source,
            "revision": state.revision,
            "modification_verified": None,
            "is_mock": False,
        }
        existing.add(smiles)
        added.append(candidate_id)
    if added:
        state.events.append({"type": "seed_import", "revision": state.revision,
                             "candidate_ids": added, "source": source})
    return added


def verify_refinement(parent_smiles: str, child_smiles: str, constraints: dict) -> dict:
    """Verify structural invariants; this does not verify a natural-language chemical claim."""
    constraints = normalize_constraints(constraints)
    parent = Chem.MolFromSmiles(parent_smiles)
    child = Chem.MolFromSmiles(child_smiles)
    if parent is None or child is None:
        return {"passed": False, "checks": [], "failures": ["invalid_structure"]}
    parent_can, child_can = Chem.MolToSmiles(parent), Chem.MolToSmiles(child)
    checks = []

    def record(name, passed, observed, limit=None):
        checks.append({"name": name, "passed": bool(passed), "observed": observed, "limit": limit})

    record("not_identical", parent_can != child_can, child_can)
    heavy_delta = abs(parent.GetNumHeavyAtoms() - child.GetNumHeavyAtoms())
    record("heavy_atom_delta", heavy_delta <= constraints["max_heavy_atom_delta"],
           heavy_delta, constraints["max_heavy_atom_delta"])

    mcs = rdFMCS.FindMCS([parent, child], ringMatchesRingOnly=True, completeRingsOnly=True, timeout=2)
    mcs_atoms = int(mcs.numAtoms)
    changed_atoms = parent.GetNumHeavyAtoms() + child.GetNumHeavyAtoms() - 2 * mcs_atoms
    record("changed_atoms", changed_atoms <= constraints["max_changed_atoms"],
           changed_atoms, constraints["max_changed_atoms"])

    changed_parent_indices = _changed_parent_atoms(parent, child, mcs.smartsString)
    changed_child_indices = _changed_parent_atoms(child, parent, mcs.smartsString)
    allowed_indices = constraints["allowed_parent_atom_indices"]
    if allowed_indices:
        in_range = all(index < parent.GetNumAtoms() for index in allowed_indices)
        site_ok = in_range and bool(changed_parent_indices) and set(changed_parent_indices) <= set(allowed_indices)
        record("allowed_edit_site", site_ok, changed_parent_indices, allowed_indices)

    fpgen = rdFingerprintGenerator.GetMorganGenerator(radius=2, fpSize=2048)
    similarity = float(DataStructs.TanimotoSimilarity(fpgen.GetFingerprint(parent), fpgen.GetFingerprint(child)))
    record("morgan_similarity", similarity >= constraints["min_similarity"],
           round(similarity, 6), constraints["min_similarity"])

    if constraints["preserve_scaffold"]:
        scaffold = MurckoScaffold.GetScaffoldForMol(parent)
        # Acyclic molecules have an empty Murcko scaffold; MCS and similarity still constrain them.
        preserved = scaffold.GetNumAtoms() == 0 or child.HasSubstructMatch(scaffold)
        record("parent_scaffold_preserved", preserved,
               Chem.MolToSmiles(scaffold) if scaffold.GetNumAtoms() else "acyclic")
    for smarts in constraints["protected_smarts"]:
        query = Chem.MolFromSmarts(smarts)
        record("protected_smarts", parent.HasSubstructMatch(query) and child.HasSubstructMatch(query), smarts)

    failures = [check["name"] for check in checks if not check["passed"]]
    return {
        "passed": not failures,
        "checks": checks,
        "failures": failures,
        "parent_smiles": parent_can,
        "child_smiles": child_can,
        "mcs_atoms": mcs_atoms,
        "changed_atoms": changed_atoms,
        "changed_parent_atom_indices": changed_parent_indices,
        "changed_child_atom_indices": changed_child_indices,
        "similarity": round(similarity, 6),
        "semantic_change_verified": False,
    }


def _changed_parent_atoms(parent, child, mcs_smarts: str) -> list[int]:
    """Estimate which parent atom environments changed using one deterministic MCS mapping."""
    query = Chem.MolFromSmarts(mcs_smarts) if mcs_smarts else None
    if query is None:
        return list(range(parent.GetNumAtoms()))
    parent_match = parent.GetSubstructMatch(query)
    child_match = child.GetSubstructMatch(query)
    if not parent_match or not child_match:
        return list(range(parent.GetNumAtoms()))
    parent_to_query = {atom_index: query_index for query_index, atom_index in enumerate(parent_match)}
    child_to_query = {atom_index: query_index for query_index, atom_index in enumerate(child_match)}

    def environment(mol, atom_index, mapping):
        atom = mol.GetAtomWithIdx(atom_index)
        neighbors = []
        for bond in atom.GetBonds():
            other = bond.GetOtherAtomIdx(atom_index)
            if other in mapping:
                neighbors.append(("mcs", mapping[other], str(bond.GetBondType())))
            else:
                neighbors.append(("external", mol.GetAtomWithIdx(other).GetAtomicNum(), str(bond.GetBondType())))
        return (atom.GetAtomicNum(), atom.GetFormalCharge(), atom.GetIsAromatic(), tuple(sorted(neighbors)))

    changed = set(range(parent.GetNumAtoms())) - set(parent_match)
    for query_index, parent_index in enumerate(parent_match):
        child_index = child_match[query_index]
        if environment(parent, parent_index, parent_to_query) != environment(child, child_index, child_to_query):
            changed.add(parent_index)
    return sorted(changed)


def candidate_goal_assessment(candidate: dict, constraints: dict) -> dict:
    constraints = normalize_constraints(constraints)
    checks = []

    def check(name, passed, observed, required):
        checks.append({"name": name, "passed": bool(passed), "observed": observed, "required": required})

    status = candidate.get("evaluation_status")
    check("successful_evaluation", status in {"complete", "screening_only"}, status,
          "complete or screening_only")
    current = candidate.get("current_validation")
    if candidate.get("parent_id"):
        check("current_structure", bool(current and current["passed"]),
              current.get("failures") if current else "not_revalidated", "current constraints")
    if constraints["require_verified_refinement"]:
        verified = bool(candidate.get("parent_id") and current and current["passed"])
        check("verified_refinement", verified, verified, True)
    if constraints["require_meaningful_improvement"]:
        outcome = candidate.get("current_improvement", {}).get("outcome")
        check("meaningful_improvement", outcome == "supported", outcome, "supported")
    if constraints["require_safety_gate"]:
        check("safety_gate", candidate.get("safety_gate_pass") is True,
              candidate.get("safety_gate_pass"), True)
    for key, field in (("min_property_score", "property_score"),
                       ("min_composite_score", "composite_score")):
        threshold = constraints[key]
        if threshold is not None:
            value = candidate.get(field)
            check(key, isinstance(value, (int, float)) and value >= threshold, value, threshold)
    threshold = constraints["max_vina_score"]
    if threshold is not None:
        value = (candidate.get("dock") or {}).get("score")
        check("max_vina_score", isinstance(value, (int, float)) and value <= threshold, value, threshold)
    return {"passed": all(item["passed"] for item in checks), "checks": checks,
            "unmet": [item["name"] for item in checks if not item["passed"]]}


def evidence_delta(parent: dict, child: dict) -> dict:
    """Return child-minus-parent deltas only when both values are comparable."""
    result = {}
    for name, getter in (
        ("property_score", lambda c: c.get("property_score")),
        ("composite_score", lambda c: c.get("composite_score")),
        ("vina", lambda c: (c.get("dock") or {}).get("score")),
        ("herg_risk", lambda c: (c.get("admet") or {}).get("herg_risk_score")),
    ):
        before, after = getter(parent), getter(child)
        result[name] = {"parent": before, "child": after,
                        "delta": after - before
                        if type(before) in (int, float) and type(after) in (int, float)
                        and math.isfinite(before) and math.isfinite(after) else None}
    result["same_protocol"] = bool(parent.get("protocol_id") and parent.get("protocol_id") == child.get("protocol_id"))
    return result
