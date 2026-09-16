# Memory Confirmatory Experiment Plan

## Locked question

Does isolated working/failed memory improve a Judge-driven EGFR design loop over the same Judge loop without memory, while keeping the continuous hERG-risk proxy within a non-inferiority margin?

## Design

- Reference: `reflection` (dual generators + Judge, no memory).
- Treatment: `reflection_memory` (same system plus isolated working and failed memory).
- 10 independent eligible repeats per group.
- 3 rounds per repeat.
- DeepSeek and MiniMax each return exactly 5 candidates per round.
- Runs are interleaved by repeat: reference 1, treatment 1, reference 2, treatment 2, and so on.
- Target, receptor, pocket, Vina seed, exhaustiveness, candidate budget, retry rules and model settings are fixed.

Interleaving limits confounding from API/provider drift and from the docking cache warming over wall-clock time.

## Scoring protocol

Protocol `multi-objective-v3.1` keeps six decimal places and separates:

- property quality: 0.55;
- normalized Vina: 0.30;
- continuous hERG safety proxy: 0.15.

Candidates also receive a non-dominated Pareto rank over Vina, property score, continuous hERG-risk proxy and synthetic accessibility. Judge and memory prioritize safety-passing Pareto candidates. A candidate passes the safety gate when hERG-risk proxy is at most 0.55 and logP is at most 4.50. The 0.55 threshold was fixed before the final confirmatory run after calibration against 237 prior evaluated candidates; it retains approximately the lowest-risk quartile while the confirmatory safety decision continues to use the continuous risk value.

The hERG value remains an explicit RDKit descriptor heuristic. It is not a calibrated probability or experimental toxicity result.
The proxy weights aliphatic amines at 1.0, resonance-deactivated aniline-like
nitrogens at 0.35 and pyridine/quinazoline-like aromatic nitrogens at 0.15.
This prevents weakly basic hinge-binding core nitrogens from being treated as
equivalent to strongly basic solubilizing tails.

## Pre-registered endpoints

Primary endpoint: `best_safe_composite_global`, the highest six-decimal composite score among candidates that pass the safety gate.

Key secondary endpoint: mean first-to-last-round Vina change. Negative values indicate improvement.

Safety endpoint: run-level mean continuous hERG-risk proxy.

## Approval rule

Long-term autonomous memory is approved only if all conditions hold:

1. Treatment improves the primary endpoint with a favorable two-sided Welch test at `p < 0.05`.
2. At least 70% of treatment runs show improvement and mean first-to-last Vina change is negative.
3. The upper bound of the 95% Welch confidence interval for `treatment - reference` mean hERG-risk is no greater than the absolute non-inferiority margin `0.05`.

Failure of any condition returns `do_not_approve`. A failed gate means evidence is insufficient for long-term memory; it does not prove that memory can never help.

## Caching and audit

Full evaluation cache entries remain scoped to the complete scoring protocol. Docking is cached separately by receptor, pocket, Vina settings, binary, preparation implementation and canonical SMILES. When the scoring formula changes, ADMET, composite and Pareto values are recomputed while compatible docking artifacts may be reused.

Incomplete provider output, Judge failure or interrupted attempts are excluded by the quality gate and retained in the benchmark manifest.

Configuration: [`experiments/confirmatory_matrix.yaml`](../experiments/confirmatory_matrix.yaml).
