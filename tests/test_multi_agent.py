"""tests/test_multi_agent.py - Unit tests for agents.multi_agent (Phase 4.6).

Covers:
- generators_are_heterogeneous
- aggregate_candidates (dedup, diversity, voting, top_n)
- combine_judge_votes (weighted vote, dispersion)
- should_enter_debate (confidence + disagreement triggers)
- validate_multi_agent_config
- agents.router (RoundFingerprint, route)
"""
from __future__ import annotations

import pytest

from agents.multi_agent import (
    AggregatedCandidate,
    JudgeVerdict,
    MultiAgentConfigError,
    aggregate_candidates,
    combine_judge_votes,
    generators_are_heterogeneous,
    should_enter_debate,
    validate_multi_agent_config,
)
from agents.router import RoundFingerprint, route, router_enabled


# ============================================================
# generators_are_heterogeneous
# ============================================================

class TestHeterogeneity:
    def test_single_generator_passes(self):
        assert generators_are_heterogeneous([{"prompt_role": "qed"}]) is True

    def test_empty_list_passes(self):
        assert generators_are_heterogeneous([]) is True

    def test_different_prompt_role_passes(self):
        gens = [
            {"prompt_role": "qed", "provider": "MiniMax"},
            {"prompt_role": "vina", "provider": "MiniMax"},
        ]
        assert generators_are_heterogeneous(gens) is True

    def test_same_prompt_role_fails(self):
        gens = [
            {"prompt_role": "qed", "provider": "MiniMax"},
            {"prompt_role": "qed", "provider": "MiniMax"},
        ]
        assert generators_are_heterogeneous(gens) is False

    def test_required_diff_axes_can_be_model(self):
        gens = [
            {"prompt_role": "qed", "model": "m1"},
            {"prompt_role": "qed", "model": "m2"},
        ]
        assert generators_are_heterogeneous(gens, required_diff_axes=("model",)) is True
        assert generators_are_heterogeneous(gens, required_diff_axes=("prompt_role",)) is False


# ============================================================
# aggregate_candidates
# ============================================================

class TestAggregate:
    def _outputs(self):
        # Two generators overlap on one SMILES; one is unique.
        return {
            "A1_qed": [
                {"smiles": "CCO", "confidence": 0.9},
                {"smiles": "c1ccccc1", "confidence": 0.7},
            ],
            "A2_vina": [
                {"smiles": "CCO", "confidence": 0.6},   # duplicate of A1
                {"smiles": "CCN", "confidence": 0.5},   # similar to CCO
            ],
        }

    def test_basic_aggregation(self):
        agg, stats = aggregate_candidates(
            self._outputs(),
            diversity_floor=0.7,
            top_n=10,
        )
        assert stats["n_input"] == 4
        assert stats["n_after_dedup"] == 3     # CCO merged across A1+A2
        # CCO has highest score (0.9); diversity floor drops CCN (Tanimoto high vs CCO)
        assert len(agg) >= 1
        assert agg[0].score == pytest.approx(0.9, abs=1e-6)
        assert "A1_qed" in agg[0].sources

    def test_multi_source_attribution(self):
        agg, _ = aggregate_candidates(self._outputs(), diversity_floor=0.0, top_n=10)
        cco = next(k for k in agg if k.smiles == "CCO")
        assert set(cco.sources) == {"A1_qed", "A2_vina"}

    def test_invalid_smiles_dropped(self):
        out = {"A1": [{"smiles": "not_a_smiles", "confidence": 1.0}]}
        agg, stats = aggregate_candidates(out)
        assert agg == []
        assert stats["n_after_dedup"] == 0

    def test_top_n_caps(self):
        out = {f"g{i}": [{"smiles": f"CCC{'C' * i}", "confidence": 1.0 - i * 0.1}] for i in range(5)}
        agg, stats = aggregate_candidates(out, diversity_floor=0.99, top_n=2)
        assert len(agg) == 2
        assert stats["n_kept"] == 2

    def test_returns_aggregated_candidate_type(self):
        agg, _ = aggregate_candidates(self._outputs(), diversity_floor=0.0, top_n=5)
        for c in agg:
            assert isinstance(c, AggregatedCandidate)


# ============================================================
# combine_judge_votes
# ============================================================

class TestMultiJudgeVote:
    def test_empty(self):
        s, per, disp = combine_judge_votes([])
        assert s == 0.0
        assert per == {}
        assert disp == 0.0

    def test_single_judge(self):
        v = [JudgeVerdict("J1", 0.8, "qed")]
        s, per, disp = combine_judge_votes(v)
        assert s == pytest.approx(0.8, abs=1e-6)
        assert per == {"J1": 0.8}
        assert disp == 0.0

    def test_weighted_vote(self):
        v = [
            JudgeVerdict("J1", 0.8, "qed"),
            JudgeVerdict("J2", 0.4, "vina"),
        ]
        s, per, disp = combine_judge_votes(v, weights=[3.0, 1.0])
        assert s == pytest.approx(0.7, abs=1e-6)
        assert per["J1"] == pytest.approx(0.6, abs=1e-6)
        assert per["J2"] == pytest.approx(0.1, abs=1e-6)
        # dispersion: max(0.8,0.4)-median(0.6)=0.2
        assert disp == pytest.approx(0.2, abs=1e-6)

    def test_zero_weights_handled(self):
        v = [JudgeVerdict("J1", 0.5, "qed")]
        s, _, _ = combine_judge_votes(v, weights=[0.0])
        assert s == 0.0


# ============================================================
# should_enter_debate
# ============================================================

class TestDebateTrigger:
    def test_no_judges(self):
        should, reason = should_enter_debate([])
        assert should is False
        assert reason == "no_judges"

    def test_low_confidence_triggers(self):
        v = [
            JudgeVerdict("J1", 0.5, "qed"),
            JudgeVerdict("J2", 0.20, "vina"),
        ]
        should, reason = should_enter_debate(v, confidence_floor=0.30)
        assert should is True
        assert "low_confidence" in reason

    def test_high_disagreement_triggers(self):
        v = [
            JudgeVerdict("J1", 0.9, "qed"),
            JudgeVerdict("J2", 0.3, "vina"),
        ]
        # median = 0.6, max = 0.9, disp = 0.3 >= 0.15
        should, reason = should_enter_debate(v, disagreement_threshold=0.15)
        assert should is True
        assert "high_disagreement" in reason

    def test_aligned_judges_no_debate(self):
        v = [
            JudgeVerdict("J1", 0.7, "qed"),
            JudgeVerdict("J2", 0.6, "vina"),
        ]
        should, reason = should_enter_debate(v)
        assert should is False
        assert reason == "ok"

    def test_three_judges_balanced_no_debate(self):
        v = [
            JudgeVerdict("J1", 0.6, "qed"),
            JudgeVerdict("J2", 0.55, "vina"),
            JudgeVerdict("J3", 0.5, "synth"),
        ]
        should, _ = should_enter_debate(v)
        assert should is False


# ============================================================
# validate_multi_agent_config
# ============================================================

class TestValidation:
    def test_none_returns_empty(self):
        assert validate_multi_agent_config(None, {}) == []

    def test_disabled_returns_empty(self):
        cfg = {"enabled": False, "generators": [{"provider": "x"}]}
        assert validate_multi_agent_config(cfg, {}) == []

    def test_empty_generators_warns(self):
        cfg = {"enabled": True, "generators": []}
        warnings = validate_multi_agent_config(cfg, {})
        assert any("generators=[]" in w for w in warnings)

    def test_unknown_provider_warns(self):
        cfg = {
            "enabled": True,
            "generators": [{"name": "A1", "provider": "no_such"}],
            "judges": [],
        }
        warnings = validate_multi_agent_config(cfg, {"MiniMax": {}})
        assert any("no_such" in w for w in warnings)

    def test_non_heterogeneous_warns(self):
        cfg = {
            "enabled": True,
            "generators": [
                {"name": "A1", "provider": "MiniMax", "prompt_role": "qed"},
                {"name": "A2", "provider": "MiniMax", "prompt_role": "qed"},
            ],
        }
        warnings = validate_multi_agent_config(cfg, {"MiniMax": {}})
        assert any("NOT heterogeneous" in w for w in warnings)

    def test_valid_heterogeneous_no_warning(self):
        cfg = {
            "enabled": True,
            "generators": [
                {"name": "A1", "provider": "MiniMax", "prompt_role": "qed"},
                {"name": "A2", "provider": "MiniMax", "prompt_role": "vina"},
            ],
            "judges": [],
            "aggregation": {"top_n": 12},
        }
        warnings = validate_multi_agent_config(cfg, {"MiniMax": {}})
        assert warnings == []


# ============================================================
# router
# ============================================================

class TestRouter:
    def test_default_experts_ok(self):
        assert route(RoundFingerprint()) == "prompt_exploit_expert"

    def test_property_weak(self):
        fp = RoundFingerprint(property_weak=True)
        assert route(fp) == "prompt_qed_expert"

    def test_vina_weak(self):
        fp = RoundFingerprint(vina_weak=True)
        assert route(fp) == "prompt_vina_expert"

    def test_sa_difficult_overrides(self):
        # sa_difficult has higher priority than property_weak
        fp = RoundFingerprint(property_weak=True, sa_difficult=True)
        assert route(fp) == "prompt_sa_expert"

    def test_custom_experts(self):
        experts = {"property_weak": "CUSTOM", "ok": "OK"}
        assert route(RoundFingerprint(property_weak=True), experts=experts) == "CUSTOM"
        assert route(RoundFingerprint(), experts=experts) == "OK"

    def test_router_enabled(self):
        assert router_enabled(None) is False
        assert router_enabled({}) is False
        assert router_enabled({"enabled": False}) is False
        assert router_enabled({"enabled": True}) is True

    def test_fingerprint_from_history_property_weak(self):
        fp = RoundFingerprint.from_history(
            best_property_history=[0.95, 0.94, 0.94],   # regressing
            best_vina_history=[-8.0, -8.5, -8.7],      # improving
        )
        assert fp.property_weak is True
        assert fp.vina_weak is False

    def test_fingerprint_from_history_vina_weak(self):
        fp = RoundFingerprint.from_history(
            best_property_history=[0.90, 0.92, 0.95],   # improving
            best_vina_history=[-8.0, -8.0, -8.0],      # flat -> not getting better
        )
        assert fp.property_weak is False
        assert fp.vina_weak is True

    def test_fingerprint_sa_difficult(self):
        fp = RoundFingerprint.from_history(
            best_property_history=[0.9, 0.95],
            best_vina_history=[-8.0, -8.5],
            recent_sa_scores=[3.5, 4.5, 5.0],  # > 4.0 threshold
        )
        assert fp.sa_difficult is True

    def test_fingerprint_handles_short_history(self):
        fp = RoundFingerprint.from_history(
            best_property_history=[0.9],
            best_vina_history=[-8.0],
        )
        # Not enough history to decide -> both flags False
        assert fp.property_weak is False
        assert fp.vina_weak is False
        assert fp.sa_difficult is False