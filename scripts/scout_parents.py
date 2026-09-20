"""Offline parent scout: find parents whose one-hop catalogue contains a qualifying product.

Why this exists
---------------
Every 2D diagnostic run so far (v2..v10, 13 runs) used the *same* parent
(``Oc1ccccc1``). The stated next step in README is a small multi-parent
stability study, but the second scenario in the script (``phenetole``) has
zero reachable qualifying products, so there was nothing to repeat on.

This script is pure local arithmetic: RDKit graph edits plus the fixed
``property_score`` protocol. It makes NO model calls and NO docking calls, so
it costs nothing and cannot pollute an experiment budget. Its output is a
*candidate list for humans to review*, not an experimental result, and the
scores it computes must never be shown to a policy (same rule as the
reachability audit).

Usage:
    python scripts/scout_parents.py --out runs/samples/parent_scout_20260920.json
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml

from agents.evaluator import evaluate_candidates
from agents.harness.editor import apply_edit
from agents.harness.molecule_ops import normalize_constraints
from agents.harness.planning import preview
from agents.harness.state import TaskState
from agents.harness.evidence import judge_effect
from agents.harness.molecule_ops import add_seed_candidates, evidence_delta
from tools.provenance import digest, evaluation_protocol

# Deliberately small, ordinary, drug-like aromatic parents. Chosen so the
# one-hop space is interpretable, not to make the task easy.
PARENTS = [
    ("phenol", "Oc1ccccc1"),
    ("phenetole", "CCOc1ccccc1"),
    ("aniline", "Nc1ccccc1"),
    ("toluene", "Cc1ccccc1"),
    ("anisole", "COc1ccccc1"),
    ("catechol", "Oc1ccccc1O"),
    ("resorcinol", "Oc1cccc(O)c1"),
    ("hydroquinone", "Oc1ccc(O)cc1"),
    ("pyridine-3-ol", "Oc1cccnc1"),
    ("4-methylphenol", "Cc1ccc(O)cc1"),
    ("4-fluorophenol", "Oc1ccc(F)cc1"),
    ("benzamide", "NC(=O)c1ccccc1"),
    ("benzonitrile", "N#Cc1ccccc1"),
    ("phenyl-acetic-acid", "OC(=O)Cc1ccccc1"),
    ("naphthalen-2-ol", "Oc1ccc2ccccc2c1"),
]

FRAGMENTS = ("C", "N", "O", "F", "Cl", "CCC", "CCO", "C(F)(F)F", "C#N", "C(=O)N")


def catalogue_for(parent_smiles: str) -> list[dict]:
    """Build a one-hop catalogue: attach a fragment at every substitutable site.

    Sites are every heavy atom of the parent, so the space is defined by the
    parent itself rather than by hand-picked chemistry.
    """
    from rdkit import Chem
    molecule = Chem.MolFromSmiles(parent_smiles)
    if molecule is None:
        raise ValueError(f"invalid parent: {parent_smiles}")
    rows = []
    for site in range(molecule.GetNumAtoms()):
        for fragment in FRAGMENTS:
            rows.append({"id": f"e{len(rows) + 1:02}",
                         "edit": {"operation": "attach_fragment",
                                  "arguments": {"atom_index": site,
                                                "fragment_smiles": fragment,
                                                "fragment_atom_index": 0}}})
    return rows


def scout_one(name, parent_smiles, config, constraints):
    """Return local, docking-free feasibility and effect statistics."""
    catalogue = catalogue_for(parent_smiles)
    state = TaskState(goal="scout", config=config, mock=True, dock_enabled=False,
                      max_steps=60, max_model_calls=0, max_evaluations=10_000,
                      constraints=constraints)
    add_seed_candidates(state, [parent_smiles], source="scout")

    structural, products, seen = [], {}, set()
    for row in catalogue:
        check = preview(state, "c1", row["edit"])
        structural.append({"id": row["id"], **{k: check.get(k) for k in
                          ("passed", "product_smiles", "failures")}})
        if check.get("passed"):
            products.setdefault(check["product_smiles"], []).append(row["id"])

    if not products:
        return {"name": name, "parent_smiles": parent_smiles,
                "catalogue_actions": len(catalogue), "structurally_valid": 0,
                "unique_valid_products": 0, "qualifying_products": 0,
                "note": "no structurally valid one-hop product"}

    values = evaluate_candidates(
        [{"smiles": s} for s in [parent_smiles, *sorted(products)]],
        config["scoring"], config["target"], dock_enabled=False)
    parent, children = values[0], values[1:]
    qualifying, deltas = [], []
    for child in children:
        decision = judge_effect(evidence_delta(parent, child), "property_score", "increase", constraints)
        deltas.append({"smiles": child["smiles"], "property_score": child["property_score"],
                       "outcome": decision["outcome"], "delta": decision["observed_delta"],
                       "catalogue_ids": products[child["smiles"]]})
        if decision["outcome"] == "supported":
            qualifying.append(child["smiles"])
    return {"name": name, "parent_smiles": parent_smiles,
            "parent_property_score": parent["property_score"],
            "catalogue_actions": len(catalogue),
            "structurally_valid": sum(r["passed"] for r in structural),
            "unique_valid_products": len(products),
            "qualifying_products": len(qualifying),
            "qualifying_smiles": qualifying,
            "best_delta": max((d["delta"] for d in deltas), default=None),
            "products": sorted(deltas, key=lambda d: -(d["delta"] or -9))}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="runs/samples/parent_scout_20260920.json")
    args = parser.parse_args()

    config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    constraints = normalize_constraints({"allow_generation": False, "allow_freeform_refine": False,
        "require_planned_edits": True, "require_option_screening": True,
        "require_verified_refinement": True, "require_meaningful_improvement": True, "max_edits": 3})

    results = []
    for name, smiles in PARENTS:
        try:
            results.append(scout_one(name, smiles, config, constraints))
        except Exception as exc:
            results.append({"name": name, "parent_smiles": smiles, "error": f"{type(exc).__name__}: {exc}"})

    usable = [r for r in results if r.get("qualifying_products", 0) > 0]
    # Keep the committed sample small: the repository convention is that only
    # compact summaries are tracked. Per-parent aggregates and the qualifying
    # SMILES are enough to audit the parent choice; the full product table is
    # reproducible offline by re-running this script.
    def lean(r):
        row = {k: v for k, v in r.items() if k != "products"}
        row["qualifying_deltas"] = [{"smiles": p["smiles"], "delta": p["delta"]}
                                    for p in r.get("products", []) if p.get("outcome") == "supported"]
        row["best_product_smiles"] = r["products"][0]["smiles"] if r.get("products") else None
        return row
    summary = {
        "schema_version": 1,
        "experiment": "parent_scout_20260920",
        "note": "Offline arithmetic only: no model calls, no docking, no experiment budget. "
                "Scores must never be shown to a policy.",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "protocol_id": digest(evaluation_protocol(config["target"], config["scoring"], False)),
        "constraints": constraints,
        "fragment_set": list(FRAGMENTS),
        "catalogue_rule": "attach each fragment at every heavy atom of the parent",
        "parents_screened": len(results),
        "parents_with_qualifying_products": len(usable),
        "usable_parents": [{"name": r["name"], "parent_smiles": r["parent_smiles"],
                            "unique_valid_products": r["unique_valid_products"],
                            "qualifying_products": r["qualifying_products"],
                            "best_delta": r["best_delta"]} for r in usable],
        "results": [lean(r) for r in results],
        "interpretation_limits": [
            "This is a screening scout, not an experiment: it says which parents have a reachable positive control.",
            "It cannot show that the agent is better or worse than the rule arm.",
            "A parent with qualifying products is a candidate for the stability study, not a result.",
            "The full per-parent product table is not committed; re-run this script to reproduce it.",
        ],
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False),
                             encoding="utf-8")
    for r in results:
        if "error" in r:
            print(f"{r['name']:22s} ERROR {r['error'][:70]}")
        else:
            print(f"{r['name']:22s} parent={r['parent_smiles']:16s} "
                  f"valid={r['structurally_valid']:3d} unique={r['unique_valid_products']:3d} "
                  f"qualifying={r['qualifying_products']:2d} best={r.get('best_delta')}")
    print(f"\nusable parents: {len(usable)}/{len(results)} -> {args.out}")


if __name__ == "__main__":
    main()
