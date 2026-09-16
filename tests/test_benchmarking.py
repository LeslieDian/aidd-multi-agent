import json
from pathlib import Path
from unittest.mock import patch

from experiments.reporting import build_report, write_report, _confirmatory_decision
from agents.generator import generate_candidates, _extract_json as extract_generator_json
from scripts.run_benchmark import assess_run_quality
from loop import load_config, run_loop


def _run_arm(root: Path, group: str, judge: bool, memory: bool) -> None:
    run_dir = root / group / "repeat_01"
    cfg = load_config()
    cfg["loop"].update({
        "judge_enabled": judge,
        "memory_enabled": memory,
        "failed_set_enabled": memory,
        "memory_namespace": f"test_{group}",
    })
    result = run_loop(
        cfg, str(run_dir), max_rounds=2, n_per_provider=1,
        dock_enabled=False, use_mock=True, verbose=False,
    )
    (run_dir / "benchmark_run.json").write_text(json.dumps({
        "group": group,
        "repeat": 1,
        "duration_seconds": 0.1,
        "status": result["status"],
    }), encoding="utf-8")


def test_judge_and_memory_can_be_disabled_for_baseline(tmp_path):
    cfg = load_config()
    cfg["loop"].update({
        "judge_enabled": False,
        "memory_enabled": False,
        "failed_set_enabled": False,
    })
    with patch("loop.judge_round", side_effect=AssertionError("judge must be disabled")):
        result = run_loop(
            cfg, str(tmp_path / "baseline"), max_rounds=2, n_per_provider=1,
            dock_enabled=False, use_mock=True, verbose=False,
        )
    assert result["rounds_completed"] == 2
    round_0 = json.loads((tmp_path / "baseline/round_0.json").read_text(encoding="utf-8"))
    manifest = json.loads((tmp_path / "baseline/manifest.json").read_text(encoding="utf-8"))
    assert round_0["judgment"]["status"] == "disabled"
    assert manifest["execution"]["judge_enabled"] is False
    assert manifest["execution"]["memory_enabled"] is False
    assert not (tmp_path / "baseline/_memory/strategy_history.json").exists()


def test_mock_matrix_aggregates_into_json_and_markdown(tmp_path):
    groups = {
        "baseline": {"description": "no judge or memory"},
        "reflection": {"description": "judge only"},
        "reflection_memory": {"description": "judge and memory"},
    }
    manifest = {
        "schema_version": 1,
        "benchmark_id": "test-matrix",
        "execution": {"primary_metric": "best_composite_global"},
        "baseline_group": "baseline",
        "groups": groups,
    }
    (tmp_path / "benchmark_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    _run_arm(tmp_path, "baseline", False, False)
    _run_arm(tmp_path, "reflection", True, False)
    _run_arm(tmp_path, "reflection_memory", True, True)

    report = build_report(tmp_path)
    assert len(report["runs"]) == 3
    assert report["groups"]["baseline"]["runs"] == 1
    assert report["groups"]["reflection_memory"]["metrics"]["valid_rate"]["mean"] == 1.0
    assert set(report["comparisons"]) == {"reflection", "reflection_memory"}
    assert "reflection_memory_vs_reflection" in report["incremental_comparisons"]
    json_path, md_path = write_report(tmp_path)
    assert json_path.exists() and md_path.exists()
    assert "Group summary" in md_path.read_text(encoding="utf-8")


def test_quality_gate_rejects_provider_failure(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "summary.json").write_text(json.dumps({
        "status": "finished", "rounds_completed": 1,
    }), encoding="utf-8")
    (run_dir / "proposals_0.json").write_text(json.dumps([
        {"provider": "deepseek", "smiles_list": ["CCO"]},
        {"provider": "MiniMax", "smiles_list": [], "error": "connection"},
    ]), encoding="utf-8")
    (run_dir / "round_0.json").write_text(json.dumps({
        "judgment": {"status": "disabled"},
    }), encoding="utf-8")
    quality = assess_run_quality(
        run_dir, 1, ["deepseek", "MiniMax"], 1, judge_enabled=False,
    )
    assert quality["eligible"] is False
    assert "round_0:MiniMax:error" in quality["reasons"]


def test_strict_generation_stops_before_evaluation(tmp_path):
    cfg = load_config()
    cfg["loop"].update({
        "judge_enabled": False,
        "memory_enabled": False,
        "failed_set_enabled": False,
        "require_all_generators": True,
        "generation_max_attempts": 1,
    })
    outputs = [
        {"provider": "deepseek", "smiles_list": ["CCO"], "usage": {}},
        {"provider": "MiniMax", "smiles_list": [], "error": "bad JSON", "usage": {}},
    ]
    with (
        patch("loop.generate_candidates", return_value=outputs),
        patch("loop.evaluate_candidates_with_cache",
              side_effect=AssertionError("evaluation must not run")),
    ):
        result = run_loop(
            cfg, str(tmp_path / "strict"), max_rounds=1, n_per_provider=1,
            dock_enabled=False, use_mock=True, verbose=False,
        )
    assert result["status"] == "error"
    assert result["stop_reason"] == "generation_incomplete"
    assert result["rounds_completed"] == 0
    assert (tmp_path / "strict/proposals_0.json").exists()
    assert not (tmp_path / "strict/round_0.json").exists()


def test_generator_retries_only_until_provider_batch_is_complete():
    incomplete = {"provider": "deepseek", "smiles_list": [], "error": "bad JSON"}
    complete = {"provider": "deepseek", "smiles_list": ["CCO"], "usage": {}}
    with patch("agents.generator.generate_with_provider",
               side_effect=[incomplete, complete]) as mocked:
        outputs = generate_candidates(
            config={"llm": {"generators": ["deepseek"]}},
            n_per_provider=1,
            max_attempts_per_provider=3,
        )
    assert mocked.call_count == 2
    assert outputs[0]["smiles_list"] == ["CCO"]
    assert outputs[0]["attempt_count"] == 2
    assert outputs[0]["prior_attempt_errors"] == ["bad JSON"]


def test_generator_json_parser_uses_first_complete_object():
    parsed = extract_generator_json(
        'prefix {not json} then {"smiles_list": ["CCO"]} trailing {"other": 1}'
    )
    assert parsed == {"smiles_list": ["CCO"]}


def test_judge_retries_without_repeating_evaluation(tmp_path):
    cfg = load_config()
    cfg["loop"].update({
        "judge_enabled": True,
        "memory_enabled": False,
        "failed_set_enabled": False,
        "judge_max_attempts": 2,
    })
    failed = {
        "status": "error", "focus": "", "weakness": "fallback",
        "summary": {"error": "bad JSON"}, "usage": {},
    }
    succeeded = {
        "status": "ok", "focus": "next", "weakness": "weak",
        "reflection": "", "confidence": 0.0, "adopted_count": 0,
        "usage": {},
    }
    with (
        patch("loop.judge_round", side_effect=[failed, succeeded]) as judge,
        patch("loop.evaluate_candidates_with_cache",
              wraps=__import__("loop").evaluate_candidates_with_cache) as evaluate,
    ):
        result = run_loop(
            cfg, str(tmp_path / "judge_retry"), max_rounds=1, n_per_provider=1,
            dock_enabled=False, use_mock=True, verbose=False,
        )
    record = json.loads((tmp_path / "judge_retry/round_0.json").read_text(encoding="utf-8"))
    assert result["status"] == "finished"
    assert judge.call_count == 2
    assert evaluate.call_count == 1
    assert record["judgment"]["attempt_count"] == 2
    assert record["judgment"]["prior_attempt_errors"] == ["bad JSON"]


def test_confirmatory_gate_requires_efficacy_stability_and_safety():
    manifest = {
        "baseline_group": "reflection",
        "confirmatory": {
            "reference_group": "reflection",
            "treatment_group": "reflection_memory",
            "safety_metric": "mean_herg_risk_score",
            "safety_noninferiority_margin": 0.05,
            "min_improved_run_rate": 0.7,
        },
    }
    groups = {
        "reflection": {"metrics": {}},
        "reflection_memory": {"metrics": {
            "run_improvement_rate": {"mean": 0.8},
            "best_vina_delta": {"mean": -0.2},
        }},
    }
    comparisons = {"reflection_memory": {
        "best_safe_composite_global": {
            "favorable": True, "statistically_significant": True,
        },
        "mean_herg_risk_score": {"delta_ci95": [-0.02, 0.04]},
    }}
    decision = _confirmatory_decision(
        manifest, groups, comparisons, "best_safe_composite_global"
    )
    assert decision["status"] == "approve_long_term_memory"

    comparisons["reflection_memory"]["mean_herg_risk_score"]["delta_ci95"] = [-0.01, 0.06]
    decision = _confirmatory_decision(
        manifest, groups, comparisons, "best_safe_composite_global"
    )
    assert decision["status"] == "do_not_approve"
    assert decision["safety_noninferior"] is False
