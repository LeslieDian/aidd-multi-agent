"""Prepare selected protein with Meeko and audit atom retention.
Dry-protein protocol: chain A, alternate location A, no HETATM records.
Template protonation is an explicit assumption, not a pKa calculation.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.provenance import file_hash, versions


def atom_keys(text, pdbqt=False):
    keys = set()
    for line in text.splitlines():
        if not line.startswith(('ATOM  ', 'HETATM')):
            continue
        hydrogen = (line.split()[-1] in ('H', 'HD', 'HS')) if pdbqt else (
            line[76:78].strip() == 'H' or line[12:16].strip().startswith('H'))
        if not hydrogen:
            keys.add((line[21:22], line[22:27], line[17:20], line[12:16].strip()))
    return keys


def pdb_to_pdbqt_rigid(input_pdb, output_pdbqt, chain='A', altloc='A'):
    source, output = Path(input_pdb), Path(output_pdbqt)
    if output.exists():
        raise FileExistsError(f'Refusing to overwrite {output}; choose a new output path')
    output.parent.mkdir(parents=True, exist_ok=True)
    selected = [line[:16] + ' ' + line[17:] for line in source.read_text().splitlines()
                if line.startswith('ATOM  ') and line[21:22] == chain
                and line[16:17] in (' ', altloc)]
    if not selected:
        raise ValueError('No protein atoms selected')
    cleaned = output.with_suffix('.input.pdb')
    cleaned.write_text('\n'.join(selected) + '\nTER\nEND\n')
    command = [sys.executable, '-m', 'meeko.cli.mk_prepare_receptor',
               '--read_pdb', str(cleaned), '-p', str(output),
               '-j', str(output.with_suffix('.meeko.json')),
               '--write_pdb', str(output.with_suffix('.prepared.pdb'))]
    result = subprocess.run(command, capture_output=True, text=True, timeout=180)
    output.with_suffix('.preparation.log').write_text(result.stdout + result.stderr, encoding='utf-8')
    if result.returncode or not output.exists():
        raise RuntimeError('Meeko preparation failed; see preparation log')
    prepared = output.read_text()
    original_keys, prepared_keys = atom_keys(cleaned.read_text()), atom_keys(prepared, True)
    missing = sorted(original_keys - prepared_keys)
    polar_h = sum(line.startswith(('ATOM  ', 'HETATM')) and line.split()[-1] == 'HD'
                  for line in prepared.splitlines())
    audit = {
        'schema_version': 1, 'protocol': 'meeko-dry-protein-v1',
        'source_path': str(source.resolve()), 'source_sha256': file_hash(source),
        'receptor_sha256': file_hash(output), 'chain': chain, 'altloc': altloc,
        'hetero_policy': 'exclude HETATM; no waters/cofactors retained',
        'protonation': 'Meeko residue templates; no pKa optimization',
        'source_heavy_atoms': len(original_keys), 'prepared_heavy_atoms': len(prepared_keys),
        'source_residues': len({k[:3] for k in original_keys}),
        'prepared_residues': len({k[:3] for k in prepared_keys}),
        'missing_heavy_atoms': missing, 'polar_hydrogens': polar_h,
        'versions': versions(), 'command': command,
        'preparation_passed': not missing and polar_h > 0,
        'binding_validation': 'requires redocking and controls',
    }
    output.with_suffix('.audit.json').write_text(json.dumps(audit, indent=2), encoding='utf-8')
    if not audit['preparation_passed']:
        raise ValueError('Receptor failed atom-retention/protonation audit')
    return audit


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('input_pdb')
    p.add_argument('output_pdbqt')
    p.add_argument('--chain', default='A')
    p.add_argument('--altloc', default='A')
    args = p.parse_args()
    print(json.dumps(pdb_to_pdbqt_rigid(args.input_pdb, args.output_pdbqt,
                                      args.chain, args.altloc), indent=2))
