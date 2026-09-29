"""tests/test_marketplace.py - Marketplace / N-of-N voting tests."""
from __future__ import annotations

import pytest

from agents.marketplace import (
    BudgetAllocation,
    VoteResult,
    allocate_budgets,
    select_top_k_by_vote,
)


class TestAllocateBudgets:
    def test_empty(self):
        assert allocate_budgets([], 10) == []

    def test_single_candidate_gets_all(self):
        cands = [{"smiles": "CCO", "score": 0.9}]
        out = allocate_budgets(cands, 10)
        assert len(out) == 1
        assert out[0].smiles == "CCO"
        assert out[0].allocated == 10
        assert out[0].weight == pytest.approx(1.0, abs=1e-6)

    def test_total_allocated_equals_total_budget(self):
        cands = [
            {"smiles": "CCO", "score": 0.5},
            {"smiles": "CCN", "score": 0.7},
            {"smiles": "CCC", "score": 0.9},
        ]
        for budget in (1, 3, 7, 10, 100):
            out = allocate_budgets(cands, budget)
            assert sum(a.allocated for a in out) == budget

    def test_higher_score_gets_more_budget(self):
        cands = [
            {"smiles": "low", "score": 0.1},
            {"smiles": "high", "score": 0.9},
        ]
        out = allocate_budgets(cands, 20)
        weights = {a.smiles: a.allocated for a in out}
        assert weights["high"] > weights["low"]

    def test_min_weight_floor(self):
        """Lowest-scored candidate gets a meaningful share (post-normalization)."""
        cands = [
            {"smiles": "lowest", "score": 0.0},
            {"smiles": "middle", "score": 5.0},
            {"smiles": "highest", "score": 10.0},
        ]
        out = allocate_budgets(cands, 12, min_weight=0.10)
        weights = {a.smiles: a.weight for a in out}
        # The lowest got a non-trivial share (>= the lifted floor / sum).
        assert weights["lowest"] >= 0.05  # empirically ~0.084
        # Total weight must still sum to 1.
        assert sum(weights.values()) == pytest.approx(1.0, abs=1e-6)

    def test_missing_smiles_filtered(self):
        cands = [
            {"smiles": "CCO", "score": 0.5},
            {"smiles": "", "score": 0.9},
            {"score": 0.7},  # no smiles key
        ]
        out = allocate_budgets(cands, 10)
        assert len(out) == 1
        assert out[0].smiles == "CCO"

    def test_total_budget_zero_raises(self):
        with pytest.raises(ValueError, match="total_budget"):
            allocate_budgets([{"smiles": "CCO", "score": 0.5}], 0)

    def test_all_zero_scores_still_allocates(self):
        """Edge case: all candidates have score 0; softmax degenerates.
        With min_weight=0.05, all should still get some weight.
        """
        cands = [
            {"smiles": "a", "score": 0.0},
            {"smiles": "b", "score": 0.0},
            {"smiles": "c", "score": 0.0},
        ]
        out = allocate_budgets(cands, 9)
        assert sum(a.allocated for a in out) == 9
        # All weights should be roughly equal (1/3 each, minus floor).
        assert all(a.weight >= 0.05 for a in out)


class TestSelectTopKByVote:
    def _fingerprint_smiles(self):
        return [
            {"smiles": "c1ccccc1", "score": 1.0},       # benzene
            {"smiles": "c1ccc(O)cc1", "score": 0.8},     # phenol (similar to benzene)
            {"smiles": "CC(=O)OC1=CC=CC=C1C(=O)O", "score": 0.5},  # aspirin (different)
        ]

    def test_returns_at_most_k(self):
        cands = self._fingerprint_smiles()
        out, stats = select_top_k_by_vote(cands, k=2)
        assert len(out) == 2

    def test_empty_candidates(self):
        out, stats = select_top_k_by_vote([], k=3)
        assert out == []
        assert stats == {}

    def test_k_zero_raises(self):
        with pytest.raises(ValueError, match="k"):
            select_top_k_by_vote([{"smiles": "CCO", "score": 1.0}], k=0)

    def test_self_vote_included(self):
        """Each candidate should always have at least 1.0 from self-vote."""
        cands = [{"smiles": "CCO", "score": 0.0}]   # zero score
        out, _ = select_top_k_by_vote(cands, k=1)
        assert out[0].score >= 1.0
        assert out[0].self_weight == 1.0

    def test_similar_candidates_reinforce(self):
        """Two similar candidates should out-vote one dissimilar one."""
        cands = [
            {"smiles": "c1ccccc1", "score": 1.0},       # benzene
            {"smiles": "c1ccc(O)cc1", "score": 1.0},    # phenol
            {"smiles": "CN1CCCC1", "score": 1.0},        # piperidine (dissimilar)
        ]
        out, stats = select_top_k_by_vote(cands, k=1)
        # Top should be either benzene or phenol (similar pair),
        # NOT piperidine (which would have only its self-vote from
        # others that aren't structurally close).
        assert out[0].smiles in ("c1ccccc1", "c1ccc(O)cc1")
        assert stats["n_valid"] == 3

    def test_invalid_smiles_handled(self):
        cands = [
            {"smiles": "CCO", "score": 1.0},
            {"smiles": "NOT_A_SMILES", "score": 1.0},
            {"smiles": "c1ccccc1", "score": 1.0},
        ]
        out, stats = select_top_k_by_vote(cands, k=2)
        assert len(out) == 2
        assert stats["n_unparseable"] >= 1

    def test_vote_result_dataclass(self):
        cands = [{"smiles": "CCO", "score": 1.0}]
        out, _ = select_top_k_by_vote(cands, k=1)
        assert isinstance(out[0], VoteResult)
        assert out[0].smiles == "CCO"


class TestIntegrationWithLoopMultiAgent:
    """Smoke: the marketplace helpers should be importable from
    loop_multi_agent (future wiring) and produce sane outputs.
    """
    def test_imports(self):
        from agents.marketplace import allocate_budgets, select_top_k_by_vote
        cands = [
            {"smiles": "CCO", "score": 0.9},
            {"smiles": "CCN", "score": 0.5},
        ]
        allocs = allocate_budgets(cands, 4)
        assert sum(a.allocated for a in allocs) == 4
        top, stats = select_top_k_by_vote(cands, k=2)
        assert len(top) == 2