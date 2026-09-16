"""Run an auditable, small redocking/control validation without an LLM.
python scripts/validate_protocol.py --output runs/protocol_validation
"""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rdkit import Chem
from rdkit.Chem import AllChem, rdMolAlign
from meeko import PDBQTMolecule, RDKitMolCreate
from tools.references import load_references
from tools.dock_score import dock_smiles, validate_receptor
from tools.provenance import file_hash
from loop import load_config


def crystal_ligand(pdb, smiles):
    lines = [l for l in Path(pdb).read_text().splitlines()
             if l.startswith('HETATM') and l[17:20] == 'AQ4' and l[21] == 'A']
    mol = Chem.MolFromPDBBlock('\n'.join(lines) + '\nEND\n', sanitize=False, removeHs=True)
    if mol is None:
        raise ValueError('No valid AQ4 ligand in chain A')
    return AllChem.AssignBondOrdersFromTemplate(Chem.MolFromSmiles(smiles), mol)


def pose_rmsds(path, crystal):
    molecules = RDKitMolCreate.from_pdbqt_mol(PDBQTMolecule.from_file(str(path), skip_typing=True))
    pose = Chem.RemoveHs(molecules[0])
    # CalcRMS uses receptor coordinates directly: no post-docking alignment.
    return [rdMolAlign.CalcRMS(pose, crystal, prbId=i) for i in range(pose.GetNumConformers())]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', required=True)
    p.add_argument('--config', default='config.yaml')
    p.add_argument('--seeds', nargs='+', type=int, default=[2026, 2027, 2028])
    p.add_argument('--exhaustiveness', type=int, default=8)
    args = p.parse_args()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    cfg = load_config(args.config)
    receptor = cfg['target']['receptor_pdbqt']
    audit = validate_receptor(receptor)
    refs = load_references()
    source = audit['source_path']
    if file_hash(source) != audit['source_sha256']:
        raise ValueError('Source PDB changed since receptor preparation')
    crystal = crystal_ligand(source, refs['erlotinib']['smiles'])
    pocket = cfg['target']['pocket']
    center = tuple(pocket['center_' + a] for a in 'xyz')
    size = tuple(pocket['size_' + a] for a in 'xyz')
    records = []
    panel = [(name, row['smiles'], 'reference') for name, row in refs.items()]
    panel += [('ibuprofen','CC(C)Cc1ccc(C(C)C(=O)O)cc1','unrelated comparator'),
              ('ethanol','CCO','small comparator')]
    for name, smiles, category in panel:
        seeds = args.seeds if name == 'erlotinib' else args.seeds[:1]
        for seed in seeds:
            result = dock_smiles(smiles, receptor, center, size, seed=seed,
                                 exhaustiveness=args.exhaustiveness, artifact_dir=output,
                                 n_poses=5, timeout=180)
            row = {'name':name, 'category':category, 'seed':seed, 'result':result}
            if name == 'erlotinib' and result['valid']:
                row['pose_rmsds_angstrom'] = pose_rmsds(result['artifacts']['poses'], crystal)
                row['top_pose_pass_2A'] = row['pose_rmsds_angstrom'][0] <= 2.
            records.append(row)
            print(name, seed, result['score'], row.get('pose_rmsds_angstrom',[])[:1], flush=True)
            (output / 'records.json').write_text(json.dumps(records, indent=2), encoding='utf-8')
    redocks = [r for r in records if r['name'] == 'erlotinib']
    report = {
        'receptor_audit':audit, 'reference_source':refs['erlotinib']['source'],
        'crystal_ligand':'1M17 chain A AQ4', 'crystal_heavy_atoms':crystal.GetNumAtoms(),
        'rmsd_method':'symmetry-corrected RDKit CalcRMS, no alignment, top-ranked pose',
        'redocking_pass_count':sum(r.get('top_pose_pass_2A',False) for r in redocks),
        'redocking_total':len(redocks), 'records':records,
        'limitations':['Small control panel does not establish enrichment or affinity accuracy.',
                       'Afatinib is covalent; ordinary Vina does not model its covalent mechanism.',
                       'Dry receptor and standard template protonation; no pKa/ensemble validation.',
                       'Scores are not directly comparable to legacy runs.'],
    }
    (output / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print('Report:', output / 'report.json')


if __name__ == '__main__':
    main()
