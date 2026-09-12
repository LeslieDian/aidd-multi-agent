"""scripts/prepare_receptor.py - Convert PDB to rigid pdbqt for Vina.

Usage:
    python scripts/prepare_receptor.py data/1M17.pdb data/1M17.pdbqt
"""
import sys

from openbabel import openbabel as ob
from openbabel import pybel


def pdb_to_pdbqt_rigid(input_pdb: str, output_pdbqt: str) -> None:
    """Convert PDB to rigid pdbqt (Vina-compatible)."""
    mol = next(pybel.readfile("pdb", input_pdb))
    mol.OBMol.StripSalts()  # remove waters/additives
    mol.removeh()            # strip old H, let Vina add later

    # Write rigid pdbqt (no torsions)
    mol.write("pdbqt", output_pdbqt, overwrite=True)
    print(f"[OK] Wrote {output_pdbqt}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python prepare_receptor.py input.pdb output.pdbqt")
        sys.exit(1)
    pdb_to_pdbqt_rigid(sys.argv[1], sys.argv[2])