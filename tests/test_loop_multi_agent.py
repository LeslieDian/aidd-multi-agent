"""tests/test_loop_multi_agent.py - Unit tests for loop_multi_agent.

The multi-agent entry point is intentionally a thin wrapper around the
multi_agent primitives; here we exercise the config readers, the
generator-call contract, the aggregation flow, and the judge-vote flow
*without* touching the LLM (use_mock=True where applicable).
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from loop_multi_agent import (
    MultiGeneratorResult,
    MultiJudgeResult,
    aggregate_round,
    call_multi_generators,
    call_multi_judges,
    maybe_route,
    multi_agent_enabled,
    read_aggregation,
    read_debate,
    read_generators,
    read_judges,
    read_multi_agent_block,
    read_router,
    run_multi_agent_loop,
    warn_if_degraded,
)
from agents.multi_agent import AggregatedCandidate, JudgeVerdict


@pytest.fixture(autouse=True)
def _clean_test_runs():
    """Best-effort cleanup of the small set of dirs this file writes to.

    The happy-path / non-heterogeneous tests re-use ``runs/_test_ma_*`` paths
    on each run; without cleanup, the second invocation trips the
    ``FileExistsError`` that ``run_multi_agent_loop`` raises when an
    output dir already contains ``round_*.json`` or ``manifest.json``.
    """
    paths = [
        Path("runs/_test_ma_happy"),
        Path("runs/_test_ma_homog"),
    ]
    for p in paths:
        if p.exists():
            shutil.rmtree(p, ignore_errors=True)
    yield


# ============================================================
# Config readers
# ============================================================

CONFIG_TEMPLATE = {
    "llm": {
        "providers": {
            "MiniMax": {"model": "MiniMax-M3"},
            "deepseek": {"model": "deepseek-chat"},
            "kimi": {"model": "kimi-k3"},
            "judge_MiniMax": {"model": "MiniMax-M3"},
        }
    },
    "loop": {
        "multi_agent": {
            "enabled": True,
            "generators": [
                {"name": "A1_qed", "provider": "MiniMax", "prompt_role": "qed"},
                {"name": "A2_vina", "provider": "deepseek", "prompt_role": "vina"},
            ],
            "judges": [
                {"name": "J1", "provider": "judge_MiniMax", "prompt_role": "judge_property"},
            ],
            "aggregation": {"diversity_floor": 0.7, "top_n": 12},
            "router": {"enabled": True, "experts": {"ok": "ok_x"}},
            "debate": {"enabled": False, "max_rounds": 3},
        }
    },
}


class TestConfigReaders:
    def test_read_block(self):
        block = read_multi_agent_block(CONFIG_TEMPLATE["loop"])
        assert block["enabled"] is True

    def test_enabled(self):
        assert multi_agent_enabled(CONFIG_TEMPLATE["loop"]) is True

    def test_disabled(self):
        cfg = {"multi_agent": {"enabled": False}}
        assert multi_agent_enabled(cfg) is False

    def test_missing_block(self):
        assert multi_agent_enabled({"other": 1}) is False

    def test_read_generators(self):
        gens = read_generators(CONFIG_TEMPLATE["loop"])
        assert len(gens) == 2
        assert gens[0]["provider"] == "MiniMax"

    def test_read_judges(self):
        js = read_judges(CONFIG_TEMPLATE["loop"])
        assert len(js) == 1

    def test_read_aggregation_defaults(self):
        cfg = read_aggregation({})
        assert cfg["diversity_floor"] == pytest.approx(0.7)
        assert cfg["top_n"] == 12
        assert cfg["dedup"] == "canonical"

    def test_read_router_defaults(self):
        r = read_router({})
        assert r["enabled"] is False
        assert "property_weak" in r["experts"]
        assert "ok" in r["experts"]

    def test_read_debate_defaults(self):
        d = read_debate({})
        assert d["max_rounds"] == 3
        assert d["disagreement_threshold"] == pytest.approx(0.15)
        assert d["confidence_floor"] == pytest.approx(0.30)


# ============================================================
# warn_if_degraded
# ============================================================

class TestWarnings:
    def test_valid_config_no_warning(self):
        warns = warn_if_degraded(CONFIG_TEMPLATE["loop"], CONFIG_TEMPLATE["llm"]["providers"])
        assert warns == []

    def test_unknown_provider_warns(self):
        cfg = {
            "multi_agent": {
                "enabled": True,
                "generators": [{"name": "A1", "provider": "no_such", "prompt_role": "qed"}],
            }
        }
        with pytest.warns(UserWarning, match="no_such"):
            warns = warn_if_degraded(cfg, CONFIG_TEMPLATE["llm"]["providers"])
        assert any("no_such" in w for w in warns)


# ============================================================
# call_multi_generators
# ============================================================

class TestCallMultiGenerators:
    def test_invalid_generator_skipped(self):
        gens = [{"name": "A1", "provider": "no_such"}]
        out = call_multi_generators(
            config=CONFIG_TEMPLATE,
            generators=gens,
            n_per_generator=2,
            focus="",
            weakness="",
            memory_context="",
            failed_prompt="",
            use_mock=True,
        )
        # Empty result, no exception raised
        assert "A1" in out
        assert out["A1"] == []


# ============================================================
# aggregate_round
# ============================================================

class TestAggregateRound:
    def test_basic(self):
        per_gen = {
            "A1": [{"smiles": "CCO", "confidence": 0.9}],
            "A2": [{"smiles": "CCN", "confidence": 0.7}],
        }
        agg, stats = aggregate_round(per_gen, aggregation_cfg={"diversity_floor": 0.7, "top_n": 5})
        assert len(agg) >= 1
        assert stats["n_input"] == 2


# ============================================================
# maybe_route
# ============================================================

class TestMaybeRoute:
    def test_disabled_returns_empty(self):
        prompt, fp = maybe_route(
            loop_cfg={},
            best_property_history=[],
            best_vina_history=[],
            recent_sa_scores=[],
        )
        assert prompt == ""
        assert fp is None

    def test_enabled_with_fingerprint(self):
        prompt, fp = maybe_route(
            loop_cfg=CONFIG_TEMPLATE["loop"],
            best_property_history=[0.95, 0.94],
            best_vina_history=[-8.0, -8.5],
            recent_sa_scores=[],
        )
        assert fp is not None
        assert prompt  # non-empty


# ============================================================
# run_multi_agent_loop
# ============================================================

class TestRunMultiAgentLoop:
    def test_empty_generators_raises(self):
        cfg = {
            "llm": {"providers": {}},
            "loop": {"multi_agent": {"enabled": True, "generators": []}},
        }
        with pytest.raises(ValueError, match="generators"):
            run_multi_agent_loop(cfg, output_dir="runs/_test_ma_empty", use_mock=True)

    def test_non_heterogeneous_raises(self):
        cfg = {
            "llm": {"providers": {"MiniMax": {}, "deepseek": {}}},
            "loop": {
                "multi_agent": {
                    "enabled": True,
                    "generators": [
                        {"name": "A1", "provider": "MiniMax", "prompt_role": "qed"},
                        {"name": "A2", "provider": "deepseek", "prompt_role": "qed"},
                    ],
                }
            },
        }
        with pytest.raises(ValueError, match="heterogeneous"):
            run_multi_agent_loop(cfg, output_dir="runs/_test_ma_homog", use_mock=True)

    def test_happy_path_with_mock_providers(self):
        """All providers are unknown -> call_multi_generators returns [] for each.
        Aggregation yields no candidates -> loop exits cleanly.
        """
        tmp = "runs/_test_ma_happy"
        cfg = {
            "llm": {"providers": {"MiniMax": {}, "deepseek": {}}},
            "loop": {
                "multi_agent": {
                    "enabled": True,
                    "generators": [
                        {"name": "A1", "provider": "MiniMax", "prompt_role": "qed"},
                        {"name": "A2", "provider": "deepseek", "prompt_role": "vina"},
                    ],
                    "judges": [],
                    "aggregation": {"diversity_floor": 0.7, "top_n": 12},
                    "router": {"enabled": False},
                    "debate": {"enabled": False},
                }
            },
        }
        result = run_multi_agent_loop(cfg, output_dir=tmp, n_per_generator=3,
                                       max_rounds=1, use_mock=True, verbose=False)
        assert result["mode"] == "multi_agent"
        assert result["n_generators"] == 2
        assert "rounds_log" in result