"""Verified reference identities shared by prompts and benchmarks."""
import json
from pathlib import Path
from rdkit import Chem
from rdkit.Chem import rdMolDescriptors
SOURCE = 'https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/cid/10184653,176870,123631/property/IsomericSMILES,MolecularFormula,InChIKey/JSON'
NAMES = {10184653: 'afatinib', 176870: 'erlotinib', 123631: 'gefitinib'}


def load_references():
    path = Path(__file__).resolve().parents[1] / 'data/reference_compounds.json'
    rows = json.loads(path.read_text(encoding='utf-8'))['PropertyTable']['Properties']
    result = {}
    for row in rows:
        mol = Chem.MolFromSmiles(row['SMILES'])
        if (mol is None or rdMolDescriptors.CalcMolFormula(mol) != row['MolecularFormula']
                or Chem.MolToInchiKey(mol) != row['InChIKey']):
            raise ValueError(f"Reference identity mismatch for CID {row['CID']}")
        result[NAMES[row['CID']]] = {**row, 'source': SOURCE, 'smiles': Chem.MolToSmiles(mol)}
    if set(result) != set(NAMES.values()):
        raise ValueError('Reference registry is incomplete')
    return result


def format_sar_for_prompt() -> str:
    """Render the curated SAR section as a compact markdown-ish block.

    Designed to be injected into a system prompt: gives the LLM named
    binding-mode facts (hinge bidentate, western aryl SAR, tail SAR) so it
    stops generating "morpholinopropoxy + quinazoline" clones and starts
    reasoning about explicit substitution positions.
    """
    path = Path(__file__).resolve().parents[1] / 'data/reference_compounds.json'
    sar = json.loads(path.read_text(encoding='utf-8')).get('sar', {})
    if not sar:
        return ""

    shared = sar.get('shared_pharmacophore', {})
    lines = ["## Curated SAR (binding-mode facts)\n"]
    if shared:
        lines.append(
            f"- **Hinge binder (shared by all 3):** {shared.get('hinge_binder', '')}"
        )
        lines.append(
            f"- **Hinge residue:** {shared.get('hinge_residue', '')}"
        )
        if shared.get('pocket_layout'):
            lines.append(f"- **Pocket layout:** {shared['pocket_layout']}")
        lines.append("")

    for drug in ('erlotinib', 'gefitinib', 'afatinib'):
        d = sar.get(drug)
        if not d:
            continue
        lines.append(f"### {drug}")
        if d.get('key_groups'):
            lines.append("- Key groups: " + "; ".join(d['key_groups']))
        if d.get('binding_notes'):
            lines.append(f"- Binding: {d['binding_notes']}")
        if d.get('suggestions'):
            lines.append("- Concrete suggestions to try:")
            for s in d['suggestions']:
                lines.append(f"    * {s}")
        if d.get('avoid'):
            lines.append("- Avoid:")
            for s in d['avoid']:
                lines.append(f"    * {s}")
        lines.append("")

    lines.append(
        "Use these facts to (a) decide which structural axis to vary in a given "
        "round (western aryl vs core vs C7 tail) and (b) defend your design "
        "rationale in the JSON. Do NOT propose covalent inhibitors unless the "
        "project explicitly requests them."
    )
    return "\n".join(lines)
