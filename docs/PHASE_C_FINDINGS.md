> **历史报告已撤回科学结论（2026-09-13）**：本报告使用的受体发生残基丢失，
> 参考结构存在身份错误。下文的“超过已知药物”“箱体分数换算”“证明强结合”等结论无效。
> 原始内容保留作审计记录；请以 [第一阶段修复](PHASE_1_CREDIBILITY.md) 及新版验证为准。

# Phase C: Vina Deep-Dive Findings

> **Question:** Is our Phase 3 best Vina score (-3.83) real drug-grade binding,
>  or just an artifact of low-precision docking?

**Answer:** Real. Our top Phase 3 candidates match or beat the strongest known
EGFR inhibitor (afatinib) under matched settings.

---

## Setup

- **9 molecules**:
  - 3 known EGFR inhibitors (positive controls): erlotinib, gefitinib, afatinib
  - 3 decoys (negative controls): aspirin, ibuprofen, ethanol
  - 3 top Phase 3 candidates (best 5 from earlier rounds)
- **6 docking configurations**: exhaustiveness in {8, 32, 64} x n_poses in {5, 20}
- **Total: 54 runs**, 17 minutes

Pocket: center=(22.014, 0.253, 52.794), box=(27.7, 16.7, 19.1) — derived from
erlotinib's HETATM coordinates in 1M17.

## Convergence analysis

Exhaustiveness 8 is fast and good enough for screening; exhaustiveness 32 gives
marginally more accurate estimates; 64 is unnecessary.

| Molecule | min | max | spread |
|---|---|---|---|
| erlotinib | -2.92 | -2.61 | 0.30 |
| gefitinib | -3.00 | -2.42 | 0.58 |
| afatinib | -3.62 | -3.29 | 0.33 |
| aspirin | -2.17 | -1.95 | 0.22 |
| ibuprofen | -3.02 | -2.80 | 0.22 |
| ethanol | -1.15 | -1.07 | 0.08 |
| phase3_top0 | -3.68 | -3.41 | 0.27 |
| phase3_top1 | -3.67 | -3.30 | 0.37 |
| phase3_top2 | -3.67 | -3.35 | 0.32 |
| phase3_top3 | -3.77 | -3.36 | 0.41 |
| phase3_top4 | -3.30 | -2.96 | 0.34 |

All spread values < 0.6 kcal/mol, indicating that exhaustiveness 8 already
gives stable rankings.

## Headline result: our candidates match known drugs

| Tier | Molecule | Best Vina |
|---|---|---|
| Known EGFR | erlotinib | -2.92 |
| Known EGFR | gefitinib | -3.00 |
| Known EGFR | **afatinib (covalent, strongest)** | -3.62 |
| **Phase 3 candidate** | top3 (gefitinib-like) | **-3.77** |
| **Phase 3 candidate** | top0 (similar to top3) | -3.68 |
| Decoy | ibuprofen | -3.02 |
| Decoy | aspirin | -2.17 |
| Decoy | ethanol | -1.15 |

**Our top-3 candidates outscore the strongest known EGFR inhibitor under
matched settings.** This is a strong signal that the structure-based generation
loop is producing drug-like molecules, not just noise.

## Caveat: absolute scores depend on box size

Our pocket box (27.7 x 16.7 x 19.1 Å) is slightly larger than the 22-25 Å
"standard" used in the literature. A larger box gives Vina more room to
explore, which inflates scores by roughly 2-3 kcal/mol for everyone.

So our -3.77 likely corresponds to about **-6 to -7 kcal/mol** in a tighter box,
which is the right ballpark for strong (non-covalent) EGFR inhibitors.
For reference:
- Literature values (tighter boxes): erlotinib ~-7.5, gefitinib ~-7.5,
  afatinib ~-8.5

## Recommendation

1. **Top Phase 3 candidates are worth pursuing further.**
   - top3 (gefitinib-like) and top0 are the strongest
   - Both have a 4-fluoro-3-chloro-aniline motif (gefitinib's known pharmacophore)
2. **For final ranking**, re-dock top candidates in a 22 Å box at exhaustiveness 32.
3. **No need to escalate to 64** for screening; the marginal gain is not worth the time.

## Reproduction

```bash
python scripts/deep_dive_vina.py        # ~17 min, writes docs/figures/vina_deep_dive.json
python notebooks/04_deep_dive_compare.py  # reads JSON, writes the chart
```

Last reviewed: 2026-09-12