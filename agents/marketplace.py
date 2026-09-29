"""agents/marketplace.py - Marketplace / N-of-N voting (Phase 4.6 stage 13).

Implements two complementary selection mechanisms used in multi-agent
loops when the pool of candidate SMILES is non-trivial:

1. **allocate_budgets(candidates, total_budget)** — each candidate acts
   as a "trader" that bids a fraction of the next round's generator
   budget proportional to its score. Higher-scoring candidates get a
   larger share; the next round can produce more analogues near those
   candidates. Lower-scoring candidates still get *some* budget (a
   floor) so we never lose diversity entirely.

2. **select_top_k_by_vote(candidates, k)** — N-of-N voting: each
   candidate casts a vote for every other candidate weighted by ECFP4
   Tanimoto similarity. Similar candidates reinforce each other
   (votes weighted *up*); very different candidates cancel out.
   This is the "vote among your neighbours" mechanism that produces a
   crowd-sourced ranking without a single judge.

Both functions are pure / deterministic and have unit tests in
`tests/test_marketplace.py`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from rdkit import Chem, DataStructs, RDLogger
from rdkit.Chem import AllChem

RDLogger.DisableLog("rdApp.*")


# ============================================================
# 1. Budget allocation (trader model)
# ============================================================

@dataclass(frozen=True)
class BudgetAllocation:
    """One candidate's share of the next round's budget."""

    smiles: str
    raw_score: float
    weight: float          # fraction of total_budget this candidate gets
    allocated: float       # absolute number of generator calls

    def __repr__(self) -> str:
        return (f"BudgetAllocation(smiles={self.smiles!r:.40}, "
                f"weight={self.weight:.3f}, allocated={self.allocated:.1f})")


def allocate_budgets(
    candidates: Sequence[dict],
    total_budget: int,
    *,
    score_key: str = "score",
    min_weight: float = 0.05,
) -> list[BudgetAllocation]:
    """Allocate the next round's generator budget across candidates.

    Each candidate receives `weight * total_budget` calls in the next
    round. Weight is the softmax of the candidate's score (with a
    `min_weight` floor to guarantee diversity). The total allocated
    budget equals `total_budget` (rounded down, with remainder
    dropped to the top-scored candidate).

    Args:
        candidates: list of dicts each carrying at least `smiles` and
            a numeric `score` (or whatever `score_key` points to).
        total_budget: total number of generator calls to allocate.
        score_key: which dict key holds the score (default "score").
        min_weight: minimum per-candidate share (default 0.05 = 5%).

    Returns:
        list of BudgetAllocation (one per candidate, sum of `allocated`
        equals `total_budget`).
    """
    if total_budget < 1:
        raise ValueError("total_budget must be >= 1")
    if not candidates:
        return []

    raw = []
    for c in candidates:
        smi = c.get("smiles")
        if not smi:
            continue
        score = float(c.get(score_key, 0.0) or 0.0)
        raw.append((smi, score))

    if not raw:
        return []

    # Softmax with temperature 1.0; subtract max for numerical stability.
    scores = [s for _, s in raw]
    m = max(scores)
    exps = [pow(2.718281828, s - m) for s in scores]
    denom = sum(exps) or 1.0
    weights = [e / denom for e in exps]

    # Apply min-weight floor. The floor is a HARD guarantee on the
    # *final* (post-normalized) weight: every candidate gets at least
    # min_weight *after* normalization. We achieve this by lifting
    # each raw weight to max(w, min_weight) and then re-normalizing
    # the whole vector back to 1.
    n = len(weights)
    effective_floor = min(min_weight, 1.0 / n)  # can't all be > 1/n
    weights = [max(w, effective_floor) for w in weights]
    total_w = sum(weights) or 1.0
    weights = [w / total_w for w in weights]

    # Allocate integer calls proportionally; give remainder to top.
    raw_alloc = [w * total_budget for w in weights]
    floors = [int(a) for a in raw_alloc]
    remainder = total_budget - sum(floors)
    # Distribute remainder by descending fractional part.
    fracs = sorted(
        range(len(raw_alloc)),
        key=lambda i: -(raw_alloc[i] - floors[i]),
    )
    for idx in fracs[:remainder]:
        floors[idx] += 1

    return [
        BudgetAllocation(
            smiles=smi,
            raw_score=score,
            weight=weights[i],
            allocated=floors[i],
        )
        for i, (smi, score) in enumerate(raw)
    ]


# ============================================================
# 2. N-of-N voting via ECFP4 Tanimoto similarity
# ============================================================

def _fingerprint(smiles: str):
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return None
    try:
        return AllChem.GetMorganFingerprintAsBitVect(mol, radius=2, nBits=2048)
    except Exception:
        return None


@dataclass(frozen=True)
class VoteResult:
    """One candidate's score under N-of-N voting."""

    smiles: str
    score: float          # sum of incoming votes weighted by Tanimoto
    incoming_votes: float
    self_weight: float     # 1.0 (each candidate votes for itself)


def select_top_k_by_vote(
    candidates: Sequence[dict],
    k: int,
    *,
    score_key: str = "score",
) -> tuple[list[VoteResult], dict]:
    """N-of-N voting ranking.

    Each candidate votes for itself (weight 1.0) and for every other
    candidate weighted by their ECFP4 Tanimoto similarity (in [0, 1]).
    Final score = self_weight + sum(similarity_j * score_j).

    This makes structurally similar candidates reinforce each other
    (similar vote weights sum up); very different candidates cancel
    each other out.

    Args:
        candidates: list of dicts each carrying at least `smiles`.
        k: how many top candidates to return.
        score_key: which dict key carries the raw score.

    Returns:
        (top_k_vote_results, stats_dict)
        top_k_vote_results: list of VoteResult sorted by score desc,
            length <= k.
        stats_dict: per-candidate map with raw_score / fingerprint_status.
    """
    if k < 1:
        raise ValueError("k must be >= 1")
    if not candidates:
        return [], {}

    valid = [c for c in candidates if c.get("smiles")]
    if not valid:
        return [], {}

    fps = [_fingerprint(c["smiles"]) for c in valid]
    raw = [float(c.get(score_key, 0.0) or 0.0) for c in valid]

    # Build similarity-weighted vote matrix (only for valid fingerprints).
    n = len(valid)
    incoming = [0.0] * n
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            if fps[i] is None or fps[j] is None:
                continue
            try:
                sim = float(DataStructs.TanimotoSimilarity(fps[i], fps[j]))
            except Exception:
                continue
            incoming[j] += sim * raw[i]

    scores = [1.0 + inc for inc in incoming]  # + self-vote
    indexed = sorted(range(n), key=lambda i: -scores[i])
    top = indexed[:k]

    stats = {
        "n_input": len(candidates),
        "n_valid": n,
        "n_unparseable": sum(1 for f in fps if f is None),
        "score_range": (min(scores), max(scores)) if scores else (0.0, 0.0),
    }

    return (
        [
            VoteResult(
                smiles=valid[i]["smiles"],
                score=scores[i],
                incoming_votes=incoming[i],
                self_weight=1.0,
            )
            for i in top
        ],
        stats,
    )