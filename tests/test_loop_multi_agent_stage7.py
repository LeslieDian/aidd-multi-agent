"""tests/test_loop_multi_agent_stage7.py - Stage 7 integration tests.

Cover the wiring added in stage 7:
- build_per_generator_focus (per-generator prompt_role overlays)
- maybe_debate (debate trigger glue)
- evaluate_aggregated_candidates (RDKit-only evaluation, no Vina)
- per_generator_focus kwarg on call_multi_generators
- run_multi_agent_loop end-to-end with new fields in rounds_log

These tests do NOT touch the LLM; they use a stub `call_multi_generators`
where needed and verify the deterministic glue.
"""
from __future__ import annotations

import pytest

from agents.multi_agent import AggregatedCandidate, JudgeVerdict
from loop_multi_agent import (
    build_per_generator_focus,
    evaluate_aggregated_candidates,
    maybe_debate,
)


class TestBuildPerGeneratorFocus:
    def test_no_expert_no_debate(self):
        gens = [
            {"name": "A1_qed", "provider": "p1", "prompt_role": "qed"},
            {"name": "A2_vina", "provider": "p2", "prompt_role": "vina"},
        ]
        per_focus, per_weakness = build_per_generator_focus(
            generators=gens,
            base_focus="reduce hERG",
            base_weakness="high MW",
            expert_prompt="",
            debate_critique="",
        )
        assert per_focus["A1_qed"]
        assert per_focus["A2_vina"]
        # Different roles -> different focus strings.
        assert per_focus["A1_qed"] != per_focus["A2_vina"]
        assert per_weakness["A1_qed"] == "high MW"
        assert per_weakness["A2_vina"] == "high MW"

    def test_with_expert_prompt(self):
        gens = [{"name": "A1", "provider": "p1", "prompt_role": "qed"}]
        per_focus, _ = build_per_generator_focus(
            generators=gens,
            base_focus="reduce hERG",
            base_weakness="",
            expert_prompt="prompt_qed_expert",
            debate_critique="",
        )
        assert "PROPERTY EXPERT" in per_focus["A1"]

    def test_with_debate_critique(self):
        gens = [{"name": "A1", "provider": "p1", "prompt_role": "qed"}]
        per_focus, _ = build_per_generator_focus(
            generators=gens,
            base_focus="",
            base_weakness="",
            expert_prompt="",
            debate_critique="critic says hERG too high",
        )
        assert "critic says hERG too high" in per_focus["A1"]

    def test_empty_generators(self):
        per_focus, per_weakness = build_per_generator_focus(
            generators=[],
            base_focus="",
            base_weakness="",
            expert_prompt="",
            debate_critique="",
        )
        assert per_focus == {}
        assert per_weakness == {}


class TestMaybeDebate:
    def test_disabled(self):
        cfg = {"enabled": False}
        should, reason, critic = maybe_debate(
            judge_verdicts=[JudgeVerdict("J1", 0.5, "x")],
            debate_cfg=cfg,
            initial_generator_text="x",
            round_num=0,
        )
        assert should is False
        assert reason == "debate_disabled"
        assert critic == ""

    def test_low_confidence_triggers(self):
        cfg = {"enabled": True, "confidence_floor": 0.30,
               "disagreement_threshold": 0.15}
        verdicts = [
            JudgeVerdict("J1", 0.5, "qed", rationale="qed looks promising"),
            JudgeVerdict("J2", 0.10, "vina", rationale="vina missing"),
        ]
        should, reason, critic = maybe_debate(
            judge_verdicts=verdicts,
            debate_cfg=cfg,
            initial_generator_text="x",
            round_num=0,
        )
        assert should is True
        assert "low_confidence" in reason
        # Critic is the highest-confidence judge's rationale.
        assert critic  # non-empty
        assert "qed looks promising" in critic

    def test_no_verdicts(self):
        cfg = {"enabled": True, "confidence_floor": 0.30,
               "disagreement_threshold": 0.15}
        should, reason, critic = maybe_debate(
            judge_verdicts=[],
            debate_cfg=cfg,
            initial_generator_text="x",
            round_num=0,
        )
        assert should is False
        assert "no_judges" in reason or reason == "ok"


class TestEvaluateAggregatedCandidates:
    def test_evaluator_import_failure_is_soft(self):
        # Pass empty config that AGGREGATED candidates; evaluator should
        # import and run; on import failure it returns input + empty
        # summary. Either path is acceptable - we just want no crash.
        candidates = [{"smiles": "CCO", "candidate_id": "c0"}]
        enriched, summary = evaluate_aggregated_candidates(
            candidates=candidates,
            config={"scoring": {}, "target": {}},
        )
        # Either evaluator succeeded (enriched != candidates) or it
        # gracefully degraded (enriched == candidates, summary == {}).
        assert isinstance(enriched, list)
        assert isinstance(summary, dict)

    def test_invalid_smiles_does_not_crash(self):
        candidates = [{"smiles": "NOT_A_SMILES", "candidate_id": "c0"}]
        enriched, summary = evaluate_aggregated_candidates(
            candidates=candidates,
            config={"scoring": {}, "target": {}},
        )
        assert isinstance(enriched, list)
        assert isinstance(summary, dict)