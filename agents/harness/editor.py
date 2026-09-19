"""Deterministic RDKit graph edits and on-demand 2D depictions."""
from __future__ import annotations

import base64

from rdkit import Chem
from rdkit.Chem import rdDepictor
from rdkit.Chem.Draw import rdMolDraw2D

from .molecule_ops import canonicalize


EDIT_OPERATIONS = {
    "attach_fragment", "replace_substituent", "remove_terminal_group",
    "replace_bioisostere", "change_bond_order",
}


def molecule_atom_table(smiles: str) -> list[dict]:
    molecule = _molecule(smiles)
    return [{"index": atom.GetIdx(), "element": atom.GetSymbol(),
             "aromatic": atom.GetIsAromatic(),
             "neighbors": [neighbor.GetIdx() for neighbor in atom.GetNeighbors()]}
            for atom in molecule.GetAtoms()]


def _molecule(smiles: str):
    molecule = Chem.MolFromSmiles(smiles)
    if molecule is None:
        raise ValueError("Parent SMILES is invalid")
    return molecule


def _atom(molecule, index: int, name: str):
    if type(index) is not int or not 0 <= index < molecule.GetNumAtoms():
        raise ValueError(f"{name} is outside the molecule atom range")
    return molecule.GetAtomWithIdx(index)


def _sanitize(editable) -> Chem.Mol:
    molecule = editable.GetMol() if isinstance(editable, Chem.RWMol) else editable
    try:
        Chem.SanitizeMol(molecule)
    except Exception as exc:
        raise ValueError(f"Edit produced an invalid valence or aromatic system: {exc}") from exc
    return molecule


def _attach(base, base_index: int, fragment_smiles: str, fragment_index: int):
    fragment = _molecule(fragment_smiles)
    _atom(base, base_index, "atom_index")
    _atom(fragment, fragment_index, "fragment_atom_index")
    combined = Chem.CombineMols(base, fragment)
    editable = Chem.RWMol(combined)
    child_fragment_index = base.GetNumAtoms() + fragment_index
    editable.AddBond(base_index, child_fragment_index, Chem.BondType.SINGLE)
    return _sanitize(editable)


def _remove_branch(molecule, core_index: int, branch_index: int):
    _atom(molecule, core_index, "atom_index")
    _atom(molecule, branch_index, "neighbor_atom_index")
    bond = molecule.GetBondBetweenAtoms(core_index, branch_index)
    if bond is None:
        raise ValueError("atom_index and neighbor_atom_index are not directly bonded")
    if bond.IsInRing():
        raise ValueError("Cannot remove a ring bond as a terminal substituent")
    remove = set()
    stack = [branch_index]
    while stack:
        current = stack.pop()
        if current == core_index:
            raise ValueError("Selected bond does not isolate a substituent branch")
        if current in remove:
            continue
        remove.add(current)
        for neighbor in molecule.GetAtomWithIdx(current).GetNeighbors():
            index = neighbor.GetIdx()
            if not (current == branch_index and index == core_index):
                stack.append(index)
    if core_index in remove or len(remove) >= molecule.GetNumAtoms():
        raise ValueError("Edit would remove the parent core")
    editable = Chem.RWMol(molecule)
    for index in sorted(remove, reverse=True):
        editable.RemoveAtom(index)
    new_core_index = core_index - sum(index < core_index for index in remove)
    return _sanitize(editable), new_core_index, sorted(remove)


def apply_edit(parent_smiles: str, operation: str, *, atom_index: int,
               neighbor_atom_index: int | None = None, fragment_smiles: str | None = None,
               fragment_atom_index: int | None = None, bond_order: str | None = None) -> tuple[str, dict]:
    """Execute one explicit graph edit and return canonical child SMILES plus an audit record."""
    if operation not in EDIT_OPERATIONS:
        raise ValueError(f"Unsupported edit operation: {operation}")
    parent = _molecule(parent_smiles)
    _atom(parent, atom_index, "atom_index")
    record = {"operation": operation, "parent_atom_index": atom_index}

    if operation == "attach_fragment":
        if fragment_smiles is None or fragment_atom_index is None:
            raise ValueError("attach_fragment requires fragment_smiles and fragment_atom_index")
        child = _attach(parent, atom_index, fragment_smiles, fragment_atom_index)
        record.update(fragment_smiles=canonicalize(fragment_smiles),
                      fragment_atom_index=fragment_atom_index,
                      bond_changes=[{"kind": "added", "parent_atom": atom_index,
                                     "fragment_atom": fragment_atom_index, "order": "SINGLE"}])
    elif operation in {"replace_substituent", "replace_bioisostere"}:
        if neighbor_atom_index is None or fragment_smiles is None or fragment_atom_index is None:
            raise ValueError(f"{operation} requires neighbor_atom_index, fragment_smiles and fragment_atom_index")
        base, new_core, removed = _remove_branch(parent, atom_index, neighbor_atom_index)
        child = _attach(base, new_core, fragment_smiles, fragment_atom_index)
        record.update(neighbor_atom_index=neighbor_atom_index, removed_parent_atom_indices=removed,
                      fragment_smiles=canonicalize(fragment_smiles), fragment_atom_index=fragment_atom_index,
                      bioisostere_claim_verified=False if operation == "replace_bioisostere" else None,
                      bond_changes=[{"kind": "removed", "atoms": [atom_index, neighbor_atom_index]},
                                    {"kind": "added", "parent_atom": atom_index,
                                     "fragment_atom": fragment_atom_index, "order": "SINGLE"}])
    elif operation == "remove_terminal_group":
        if neighbor_atom_index is None:
            raise ValueError("remove_terminal_group requires neighbor_atom_index")
        child, _, removed = _remove_branch(parent, atom_index, neighbor_atom_index)
        record.update(neighbor_atom_index=neighbor_atom_index, removed_parent_atom_indices=removed,
                      bond_changes=[{"kind": "removed", "atoms": [atom_index, neighbor_atom_index]}])
    else:
        if neighbor_atom_index is None:
            raise ValueError("change_bond_order requires neighbor_atom_index")
        _atom(parent, neighbor_atom_index, "neighbor_atom_index")
        order = {"SINGLE": Chem.BondType.SINGLE, "DOUBLE": Chem.BondType.DOUBLE,
                 "TRIPLE": Chem.BondType.TRIPLE}.get((bond_order or "").upper())
        if order is None:
            raise ValueError("bond_order must be SINGLE, DOUBLE or TRIPLE")
        bond = parent.GetBondBetweenAtoms(atom_index, neighbor_atom_index)
        if bond is None:
            raise ValueError("Selected atoms are not directly bonded")
        before = str(bond.GetBondType())
        editable = Chem.RWMol(parent)
        editable.GetBondBetweenAtoms(atom_index, neighbor_atom_index).SetBondType(order)
        child = _sanitize(editable)
        record.update(neighbor_atom_index=neighbor_atom_index, bond_order=order.name,
                      bond_changes=[{"kind": "order_changed", "atoms": [atom_index, neighbor_atom_index],
                                     "before": before, "after": order.name}])

    child_smiles = Chem.MolToSmiles(child)
    if child_smiles == canonicalize(parent_smiles):
        raise ValueError("Edit did not change the molecule")
    record["child_smiles"] = child_smiles
    return child_smiles, record


def molecule_svg_data_url(smiles: str, highlight_atoms: list[int] | None = None) -> str:
    """Render a safe server-generated SVG with stable RDKit atom indices."""
    molecule = _molecule(smiles)
    rdDepictor.Compute2DCoords(molecule)
    drawer = rdMolDraw2D.MolDraw2DSVG(420, 280)
    drawer.drawOptions().addAtomIndices = True
    valid = [index for index in (highlight_atoms or []) if 0 <= index < molecule.GetNumAtoms()]
    selected = set(valid)
    highlight_bonds = [bond.GetIdx() for bond in molecule.GetBonds()
                       if bond.GetBeginAtomIdx() in selected and bond.GetEndAtomIdx() in selected]
    drawer.DrawMolecule(molecule, highlightAtoms=valid, highlightBonds=highlight_bonds)
    drawer.FinishDrawing()
    encoded = base64.b64encode(drawer.GetDrawingText().encode("utf-8")).decode("ascii")
    return "data:image/svg+xml;base64," + encoded
