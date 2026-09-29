"""tests/test_debate_recall.py - Tests for debate generator re-call (stage 10).

Covers:
- debate_recall_generators constructs the right REVISION REQUEST prompt
- debate_recall_generators falls back gracefully on generator failure
- The re-call results merge into the per-generator pool
- Re-aggregated pool is diversity-filtered correctly
"""
from __future__ import annotations

import pytest

from agents.multi_agent import AggregatedCandidate, JudgeVerdict
from loop_multi_agent import (
    aggregate_round,
    debate_recall_generators,
)


class TestDebateRecallPrompt:
    def test_revision_request_marker(self, monkeypatch):
        """debate_recall_generators should embed REVISION REQUEST in focus."""
        captured = {}

        def fake_call(*, config, generators, n_per_generator, focus, weakness,
                       memory_context, failed_prompt, parents_block,
                       use_mock, max_attempts_per_provider,
                       per_generator_focus=None, per_generator_weakness=None):
            captured["focus"] = focus
            captured["per_generator_focus"] = per_generator_focus
            return {"A1_qed": [{"smiles": "CCO", "confidence": 0.5}]}

        import loop_multi_agent
        monkeypatch.setattr(loop_multi_agent, "call_multi_generators", fake_call)

        gens = [{"name": "A1_qed", "provider": "MiniMax", "prompt_role": "qed"}]
        out = debate_recall_generators(
            config={"llm": {"providers": {"MiniMax": {}}}},
            generators=gens,
            n_per_generator=2,
            base_focus="reduce hERG",
            base_weakness="high MW",
            critic_text="hERG too high, remove basic amine",
            memory_context="",
            parents_block="",
            use_mock=False,
            max_rounds=3,
        )
        assert "A1_qed" in out
        assert "REVISION REQUEST FROM CRITIC" in captured["focus"]
        assert "hERG too high, remove basic amine" in captured["focus"]
        assert "reduce hERG" in captured["focus"]

    def test_generator_failure_returns_empty_dict(self, monkeypatch):
        """If call_multi_generators raises, debate_recall_generators returns {}."""
        def fake_call(**kwargs):
            raise RuntimeError("simulated LLM failure")

        import loop_multi_agent
        monkeypatch.setattr(loop_multi_agent, "call_multi_generators", fake_call)

        out = debate_recall_generators(
            config={}, generators=[{"name": "x", "provider": "y"}],
            n_per_generator=2, base_focus="", base_weakness="",
            critic_text="c", memory_context="", parents_block="",
            use_mock=True, max_rounds=3,
        )
        assert out == {}


class TestDebateMergeAndReAggregate:
    def test_merge_and_re_aggregate(self):
        """Simulate debate re-call merging into per-generator pool and
        re-aggregating with diversity filter.
        """
        per_gen = {
            "A1_qed": [{"smiles": "CCO", "confidence": 0.9}],
            "A2_synth": [{"smiles": "CCN", "confidence": 0.7}],
        }
        # Debate re-call adds revised candidates to the same generators.
        per_gen["A1_qed"].append({"smiles": "COc1ccccc1", "confidence": 0.8})
        per_gen["A2_synth"].append({"smiles": "Cc1ccccc1", "confidence": 0.6})

        agg, stats = aggregate_round(
            per_gen,
            aggregation_cfg={"diversity_floor": 0.5, "top_n": 10},
        )
        # 4 unique SMILES, all canonical
        assert stats["n_input"] == 4
        # Diversity filter may drop some benzene variants; at least 1 kept.
        assert stats["n_kept"] >= 1
        assert all(isinstance(c, AggregatedCandidate) for c in agg)


class TestMaybeDebateIntegration:
    """Smoke: the loop should call debate_recall_generators when triggered."""

    def test_maybe_debate_returns_critic_text(self):
        """maybe_debate should return the highest-confidence judge's rationale
        when triggered (this is what gets injected into the debate prompt).
        """
        from loop_multi_agent import maybe_debate
        verdicts = [
            JudgeVerdict("J1", 0.5, "qed", "qed looks promising"),
            JudgeVerdict("J2", 0.10, "vina", "vina weak"),
        ]
        cfg = {"enabled": True, "confidence_floor": 0.30,
               "disagreement_threshold": 0.15}
        should, reason, critic = maybe_debate(
            judge_verdicts=verdicts, debate_cfg=cfg,
            initial_generator_text="x", round_num=0,
        )
        assert should is True
        assert "low_confidence" in reason
        assert "qed looks promising" in critic