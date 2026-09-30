"""tests/test_loop_multi_agent_marketplace_integration.py

Verifies the marketplace N-of-N voting is wired into PARENTS block
selection (Phase 4.6 stage 13). Earlier loop_multi_agent.py used
``loop._select_safety_pareto_parents`` directly; after the integration,
the top-k parents are picked by ECFP4 Tanimoto N-of-N voting over the
top-2k Pareto parents.
"""
from __future__ import annotations

import pytest

from agents.marketplace import (
    allocate_budgets,
    select_top_k_by_vote,
)
from loop_multi_agent import _select_safety_pareto_parents_via_marketplace


class TestMarketplaceSelectionHelper:
    """The integration helper extracts SMILES + composite_score and
    feeds them through select_top_k_by_vote. Verify the contract.
    """

    def test_empty_history_returns_empty(self):
        out, stats = _select_safety_pareto_parents_via_marketplace(
            enriched_history=[],
            parents_k=3,
        )
        assert out == []
        assert stats == {}

    def test_single_round_with_safe_gated_candidates(self):
        """When history contains safety-pass candidates, the helper
        returns the top-k via N-of-N voting, not raw Pareto order.
        """
        # benzene + phenol are structurally similar; aspirin is the
        # outsider. Marketplace N-of-N voting should keep similar ones
        # in the top-k rather than pure score order.
        enriched_history = [[
            {"smiles": "c1ccccc1", "safety_gate_pass": True,
             "composite_score": 0.7},
            {"smiles": "c1ccc(O)cc1", "safety_gate_pass": True,
             "composite_score": 0.6},
            {"smiles": "CC(=O)Oc1ccccc1", "safety_gate_pass": True,
             "composite_score": 0.9},
        ]]
        out, stats = _select_safety_pareto_parents_via_marketplace(
            enriched_history=enriched_history,
            parents_k=2,
        )
        assert len(out) == 2
        assert stats["n_candidate_pool"] >= 2
        assert stats["n_selected"] == 2
        # N-of-N voting should put the two structurally similar
        # candidates first, NOT the outsider with the higher raw score.
        smis = [c["smiles"] for c in out]
        assert "c1ccccc1" in smis
        assert "c1ccc(O)cc1" in smis

    def test_unsafe_candidates_filtered_out(self):
        enriched_history = [[
            {"smiles": "c1ccccc1", "safety_gate_pass": True,
             "composite_score": 0.7},
            {"smiles": "CC(=O)Oc1ccccc1", "safety_gate_pass": False,
             "composite_score": 0.9},
        ]]
        out, stats = _select_safety_pareto_parents_via_marketplace(
            enriched_history=enriched_history,
            parents_k=3,
        )
        # Only the safety-pass candidate remains.
        assert len(out) == 1
        assert out[0]["smiles"] == "c1ccccc1"

    def test_round_2_uses_round_1_results(self):
        enriched_history = [
            [],
            [{"smiles": "c1ccccc1", "safety_gate_pass": True,
              "composite_score": 0.7}],
        ]
        out, _stats = _select_safety_pareto_parents_via_marketplace(
            enriched_history=enriched_history,
            parents_k=3,
        )
        assert len(out) == 1


class TestAllocateBudgetsStillWorks:
    """The marketplace trader-budget hook is independent of the parent
    selector. Make sure both pieces coexist.
    """
    def test_allocate_budgets_with_marketplace_selection(self):
        # Simulate a per-round pool; let marketplace pick top-2, then
        # allocate next-round budget across them.
        pool = [
            {"smiles": "c1ccccc1", "score": 0.9},
            {"smiles": "c1ccc(O)cc1", "score": 0.7},
            {"smiles": "CC(=O)Oc1ccccc1", "score": 0.5},
        ]
        top, _stats = select_top_k_by_vote(pool, k=2)
        # select_top_k_by_vote returns VoteResult dataclasses; convert
        # to the dict shape allocate_budgets expects.
        top_dicts = [{"smiles": v.smiles, "score": v.score} for v in top]
        allocs = allocate_budgets(top_dicts, total_budget=8)
        # 1. Budget fully allocated.
        assert sum(a.allocated for a in allocs) == 8
        # 2. allocate_budgets respects the min_weight floor (no
        #    candidate receives zero generator calls).
        for a in allocs:
            assert a.allocated > 0
        # 3. The two picks (whatever they are) sum to total budget.
        assert len({a.smiles for a in allocs}) == 2