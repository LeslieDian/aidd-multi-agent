# Notebooks (Experiment Reports)

Phase 4 three core figures are produced here. Read this first, then launch `jupyter lab`.

---

## Three figures

| Notebook | Plot | Proof |
|---|---|---|
| `01_legality_curve.py` | Valid ratio vs round | Feedback loop works |
| `02_score_curve.py` | Avg Vina/ADMET score vs round | Multi-objective optimization works |
| `03_diversity_compare.py` | Unique scaffolds per model | Heterogeneous generation is diverse |

Each plot exports PNG to `../docs/figures/`.

---

## How to run

### Option A: direct script

```bash
python scripts/run_experiments.py
```

### Option B: Jupyter Lab

```bash
pip install jupyter matplotlib
jupyter lab
```

---

## Data source

- Input: Phase 2/3 loop outputs `runs/round_0.json` ~ `runs/round_4.json`
- Each round JSON structure:

```json
{
  "round": 0,
  "target": "EGFR",
  "candidates": [
    {
      "smiles": "CC(=O)Oc1ccccc1C(=O)O",
      "model": "A1",
      "validate": {"valid": true, "mw": 180.16, "lipinski_pass": true},
      "admet": {"summary_score": 0.85, "qed": 0.55},
      "dock": {"score": -7.2, "valid": true},
      "scaffold": "O=C(O)c1ccccc1OC(C)=O"
    }
  ]
}
```

Phase 2/3 must be completed first to get real data. These notebooks include mock-data demo paths.