"""Extract ligand center from a PDB file.

Usage: python scripts/extract_ligand_center.py data/1M17.pdb [HET_CODE]

Default HET_CODE: AQ4 (erlotinib in 1M17).
If no HET_CODE is given, scans for the most common non-water HET code.

Output:
    center = (x, y, z)
    bbox   = (size_x, size_y, size_z)  -- tight box around ligand atoms
"""
import sys
from collections import Counter
from pathlib import Path


WATER_CODES = {"HOH", "WAT", "DOD", "H2O", "TIP", "TIP3"}


def find_ligand_het_code(pdb_path: Path) -> str | None:
    """Pick the most frequent non-water HET code."""
    codes: Counter = Counter()
    with pdb_path.open(encoding="utf-8") as f:
        for line in f:
            if line.startswith("HETATM"):
                code = line[17:20].strip()
                if code and code not in WATER_CODES:
                    codes[code] += 1
    if not codes:
        return None
    return codes.most_common(1)[0][0]


def extract_ligand_box(pdb_path: Path, het_code: str | None = None):
    """Compute the geometric center and bounding box of a ligand."""
    if het_code is None:
        het_code = find_ligand_het_code(pdb_path)
    if het_code is None:
        raise ValueError(f"No HETATM ligand found in {pdb_path}")

    coords = []
    with pdb_path.open(encoding="utf-8") as f:
        for line in f:
            if line.startswith("HETATM") and line[17:20].strip() == het_code:
                try:
                    x = float(line[30:38])
                    y = float(line[38:46])
                    z = float(line[46:54])
                    coords.append((x, y, z))
                except ValueError:
                    continue

    if not coords:
        raise ValueError(f"No atoms found for HET code {het_code}")

    xs = [c[0] for c in coords]
    ys = [c[1] for c in coords]
    zs = [c[2] for c in coords]
    cx = sum(xs) / len(xs)
    cy = sum(ys) / len(ys)
    cz = sum(zs) / len(zs)

    # Tight bbox with 8 A padding on each side (typical for Vina)
    pad = 8.0
    size_x = (max(xs) - min(xs)) + 2 * pad
    size_y = (max(ys) - min(ys)) + 2 * pad
    size_z = (max(zs) - min(zs)) + 2 * pad

    return {
        "het_code": het_code,
        "n_atoms": len(coords),
        "center": (round(cx, 3), round(cy, 3), round(cz, 3)),
        "size": (round(size_x, 3), round(size_y, 3), round(size_z, 3)),
        "extents": (
            (round(min(xs), 3), round(max(xs), 3)),
            (round(min(ys), 3), round(max(ys), 3)),
            (round(min(zs), 3), round(max(zs), 3)),
        ),
    }


def main():
    if len(sys.argv) < 2:
        print("Usage: python extract_ligand_center.py pdb_file [HET_CODE]")
        sys.exit(1)
    pdb_path = Path(sys.argv[1])
    het = sys.argv[2] if len(sys.argv) > 2 else None
    result = extract_ligand_box(pdb_path, het)
    print(f"HET code: {result['het_code']} ({result['n_atoms']} atoms)")
    print(f"Center: ({result['center'][0]}, {result['center'][1]}, {result['center'][2]})")
    print(f"Size:   ({result['size'][0]}, {result['size'][1]}, {result['size'][2]})")
    print(f"X range: {result['extents'][0][0]} .. {result['extents'][0][1]}")
    print(f"Y range: {result['extents'][1][0]} .. {result['extents'][1][1]}")
    print(f"Z range: {result['extents'][2][0]} .. {result['extents'][2][1]}")


if __name__ == "__main__":
    main()