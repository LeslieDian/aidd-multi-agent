> 2026-09-13 更新：第一阶段可信度问题已修复并新增回归测试，见
> [PHASE_1_CREDIBILITY.md](PHASE_1_CREDIBILITY.md)。以下原始问题列表保留历史上下文；
> 其中旧规模估计、API 域名大小写解释和立体化学断言未经验证，不作为当前实现依据。

# AIDD Multi-Agent — Known Issues & Roadmap

> **Status**: living document. Updated as issues are discovered/resolved.
> **Last review**: 2026-09-13

This document tracks every known issue, design limitation, and improvement
opportunity in the AIDD multi-agent system. Issues are categorized by severity
(P0 = blocker, P3 = nice-to-have) and grouped by component.

---

## Quick Reference

| Severity | Open | Resolved (2026-09-14) |
|---|---|---|
| P0 blocker | 1 | 2 (MiniMax 401, failed_ligands cap, WorkingMem persist) |
| P1 important | 3 | 1 (best_molecules persist + reflection sees molecules) |
| P2 polish | 2 | 1 (thresholds to config) |
| P3 future | 4 | 0 |

---

## P0 — Blocker (must fix before project is shippable)

### P0-1. `MiniMax` 401 — RESOLVED 2026-09-13

**Symptom**: All calls to `https://api.minimaxi.com/v1` returned 401
"invalid api key (2049)" with the user's `MiniMax_API_KEY`.

**Root cause**: The official MiniMax docs at the time referenced
`https://api.minimax.io/v1`, but the user's actual account endpoint is
`https://api.minimaxi.com/v1`. The endpoint is **case-sensitive**;
`api.minimaxi.com` (mixed case) works, `api.minimax.io` (lowercase)
returns 401 for this account.

**Fix**: Updated `config.yaml` to use `https://api.minimaxi.com/v1`.
Verified by direct OpenAI client test: MiniMax-Text-01 + MiniMax-M3 both return 200.

**Verified working endpoints** (from 2026-09-13 diagnostic):
- `https://api.minimaxi.com/v1`  ✅ (this user)
- `https://api.MiniMax.chat/v1`  ✅ (also works for some accounts)
- `https://api.minimax.io/v1`    ❌ 401 for this user
- `https://api.minimaxi.chat/v1` ❌ 401 for this user

---

### P0-2. `failed_ligands.json` grows unbounded

**Symptom**: `memory/failed_ligands.json` accumulates one entry per failed
SMILES with no upper bound. After ~100 loops × 50 candidates = 5000 entries,
the file grows to ~10MB and the prompt-injection size explodes.

**Impact**:
- Prompt budget exhausted (`failed_prompt` exceeds 4k tokens)
- Generator ignores the warning once the list is too long
- Disk space over months of heavy use

**Fix options** (need to pick one):
- A) **LRU eviction**: cap at `MAX_SIZE=500`, evict oldest when full
- B) **Bucket by target**: `failed_ligands/EGFR.json`, `failed_ligands/BRAF.json`
- C) **Embedding similarity** (Phase 4.3): store embeddings, retrieve top-k similar
- D) **Hybrid**: A + per-target buckets

**Recommendation**: A first (30 min), then C in Phase 4.3.

**Status**: ✅ RESOLVED 2026-09-14 (option A). `FailedLigandSet(max_size=N)`
evicts oldest insertion (LRF) when full. Default cap configured at
`scoring.failed_set.max_size: 500` in `config.yaml`. `max_size=0` keeps
legacy unbounded behavior for tests.

---

### P0-3. `WorkingMemory.max_recent = 3` loses early-round context

**Symptom**: After round N > 3, all rounds 0..N-3 are evicted from
`recent_rounds` and `strategy_chain`. Generator never sees early wisdom
like "morpholine on EGFR west ring worked".

**Fix options**:
- A) **Cross-session persistence** for `strategy_chain` →
  `memory/strategy_history.json`, only inject last 3 in prompt but
  keep full history on disk (~30 min)
- B) **Adaptive window**: grow window if best Vina is still improving
- C) **Embedding-retrieved top-K**: only inject rounds similar to current state

**Recommendation**: A first (lowest cost, highest ROI).

**Status**: ✅ RESOLVED 2026-09-14 (option A). `WorkingMemory` writes the full
strategy chain to `memory/strategy_history/<target>/<protocol_id>.json` on every
round; on restart the live `strategy_chain` seeds itself from the last
`max_recent` entries of the on-disk file. Prompt budget is still protected.

---

## P1 — Important (should fix soon)

### P1-1. `best_molecules.json` not persisted

**Symptom**: `WorkingMemory.best_so_far` is session-scoped (RAM only).
A good molecule found today is forgotten tomorrow.

**Fix**: Persist to `memory/best_molecules.json` after each round.
On startup, `WorkingMemory.__init__` loads the previous session's best.

**Status**: ✅ RESOLVED 2026-09-14. `WorkingMemory(best_persist_path=...)`
writes a per-target record to `memory/best_molecules.json` whenever
`best_so_far` improves; on next session it loads the prior best so the
generator can be told "current best Vina is -3.77" even if this session
hasn't found anything better yet. Per-target keying allows multi-target use.

---

### P1-2. Failed-set is exact-match only

**Symptom**: `FailedLigandSet.is_failed()` uses `canonical_smiles`
equality. If generator produces a *similar* (but not identical)
molecule to a previously-failed one, it slips through.

**Example**: failed set has `COc1cc2ncnc(Nc3ccc(F)c(Cl)c3)c2cc1OCCN`
but generator produces `COc1cc2ncnc(Nc3ccc(F)c(Cl)c3)c2cc1OCCCN` (one CH2
longer) — passes through despite being effectively a repeat.

**Fix**: Add embedding-based similarity check (Phase 4.3 candidate).

---

### P1-3. `adopted_count` is LLM-estimated, not measured

**Symptom**: `judgment.adopted_count` is whatever the Judge LLM claims
in its JSON output. The Judge may over/under-count.

**Fix**: Deterministic structural check — for each candidate,
compute Tanimoto similarity to previous-round best, count as "adopted"
if similarity > 0.7. Compare to LLM estimate as a sanity metric.

---

### P1-4. Reflection only sees previous focus text, not actual molecules

**Symptom**: `judge_round(previous_focus=..., previous_summary=...)`
gives Judge only the *text* of the previous suggestion, not the SMILES
of the previous molecules. Judge can't truly verify adoption.

**Fix**: Include previous round's top-3 SMILES + scores in the
"previous context" block Judge receives.

**Status**: ✅ RESOLVED 2026-09-14. `judge_round()` now accepts a
`previous_enriched` argument (loop wires it from the prior round's full
candidate list). The reflection prompt renders every prior molecule
with `SMILES + scaffold + provider + MW + logP + ADMET + Vina` so the
Judge can truly verify whether the prior focus was adopted. Fallback
to `previous_summary.top_candidates` preserved for back-compat.

**Side effect**: Phase C findings (PHASE_C_FINDINGS.md) attributed the
flat best-Vina curve to receptor issues; P1-4 unblocks a different root
cause: the Judge could not see prior molecules, so its reflections
were generic. With P1-4 the Judge can say "your morpholine suggestion
worked — R1 had 2/6 morpholine-bearing molecules and Vina improved by
0.3" instead of hand-waving.

---

## P2 — Polish (nice-to-have)

### P2-1. Thresholds hard-coded

**Symptom**: `failed_set.py` has `composite < 0.5` and `vina > -2.5`
hard-coded. Different targets need different thresholds (e.g., a
fragment binding site has different "failure" semantics than
EGFR's deep pocket).

**Fix**: Move thresholds to `config.yaml` under `target.scoring`:
```yaml
target:
  scoring:
    composite_floor: 0.5
    vina_floor: -2.5
```

**Status**: ✅ RESOLVED 2026-09-14. New `scoring.failed_set` block in
`config.yaml` exposes `composite_floor`, `vina_floor`, `max_size`.
`loop.py` reads them with fallback to the legacy `loop.failed_*` keys
for back-compat. Code defaults still present (so existing tests pass
without a config).

---

### P2-2. HITL prompts mix English/Chinese

**Symptom**: `hitl.py` outputs:
```
[HITL] Pre-loop approval required. Continue? [y/n]
```
but if user responds with a Chinese character or "是", it's rejected.

**Fix**: Accept `y/yes/n/no/yes/no/yes/no/yes/no` style + 中文 `是/否`.

---

### P2-3. No Agent-level evaluation framework

**Symptom**: Cannot quantitatively answer "is the agent actually
learning across rounds?". We have `adoption_rate` and `vina_curve`
but no integrated "agent improvement rate" metric.

**Fix**: Add `metrics.json` output per loop with:
- `valid_rate_improvement` (R0 vs final)
- `adoption_rate_avg` (across rounds)
- `best_vina_delta` (R0 best vs final best)
- `scaffold_diversity_curve`

---

## P3 — Future research

### P3-1. ADMET is RDKit-derived, not model-derived

**Symptom**: `tools/admet_score.py` uses simple RDKit descriptors
(TPSA, logP, rotatable bonds) as proxies. Real ADMET (e.g., admetSAR
or SwissADME) would be more accurate.

**Fix**: Add `admet_sar.py` calling admetSAR web service.

---

### P3-2. No molecular dynamics (MD) validation

**Symptom**: Vina gives a static snapshot score. Real binding
requires MD simulation (GROMACS / AMBER) for stability over time.

**Fix**: Optional MD plugin for top candidates.

---

### P3-3. Stereochemistry ignored

**Symptom**: All SMILES treated as 2D; stereochemistry not
considered. Real drugs are chiral.

**Fix**: Use 3D-aware canonicalization + R/S annotation.

---

### P3-4. No memory visualization

**Symptom**: User cannot easily see what the agent has learned.
`memory/failed_ligands.json` is opaque.

**Fix**: `scripts/visualize_memory.py` — pie chart of failure
categories, line plot of best-Vina-over-time, scaffold network graph.

---

## Resolved

| ID | Title | Resolved | Commit |
|---|---|---|---|
| P0-1 | MiniMax 401 | 2026-09-13 | (this doc) |
| P0-2 | failed_ligands unbounded growth | 2026-09-14 | (Phase 4.3) |
| P0-3 | WorkingMemory strategy_chain window too narrow | 2026-09-14 | (Phase 4.3) |
| P1-1 | best_molecules not persisted | 2026-09-14 | (Phase 4.3) |
| P1-4 | Reflection only sees focus text | 2026-09-14 | (Phase 4.3) |
| P2-1 | Thresholds hard-coded | 2026-09-14 | (Phase 4.3) |
| Loop Ctrl | `should_stop` only checked pre-round | 2026-09-13 | (loop.py fix) |
| Phase4.1 | Foundation (WorkingMem + FailedSet + LoopCtrl + HITL) | 2026-09-13 | `382e7ae` |
| Phase4.2 | Self-Reflection Judge | 2026-09-13 | `023b9f2` |
| Phase C | Vina deep-dive validation | 2026-09-13 | `5774ad9` |

---

## How to add a new issue

When you find something broken or unclear, append to this doc:

```markdown
### P?-N. <Short title>

**Symptom**: ...
**Impact**: ...
**Fix**: ...
**Recommendation**: ...
```

Then optionally create a GitHub issue linking to this section.