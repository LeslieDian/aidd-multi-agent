import json
from pathlib import Path
from unittest.mock import patch

import yaml

from agents.evaluator import evaluate_candidates, _resolve_docking_workers
from agents.failed_set import FailedLigandSet
from scripts.run_benchmark import assess_confirmatory_futility, deep_merge
from tools.provenance import digest, evaluation_protocol


def _config():
    return {
        "lipinski": {},
        "sa_score": {"reject_above": 6.0},
        "objective": {"precision": 6},
        "pareto": {"safety": {"max_herg_risk_score": 1.0, "max_logp": 10.0}},
        "vina": {
            "accept_below": -7.0,
            "exhaustiveness": 16,
            "n_poses": 5,
            "seed": 2026,
            "cpu": 2,
            "workers": 4,
            "timeout": 180,
        },
        "funnel": {
            "enabled": True,
            "full_dock_fraction": 0.5,
            "min_full_dock": 1,
            "fast_vina": {
                "exhaustiveness": 4,
                "n_poses": 1,
                "cpu": 1,
                "workers": 4,
                "timeout": 60,
            },
        },
    }


def _target():
    return {
        "name": "EGFR",
        "receptor_pdbqt": "unused.pdbqt",
        "pocket": {
            "center_x": 1.0, "center_y": 2.0, "center_z": 3.0,
            "size_x": 20.0, "size_y": 20.0, "size_z": 20.0,
        },
    }


def test_two_stage_funnel_fast_docks_all_and_promotes_fraction():
    calls = []

    def fake_dock(smiles, *_args, **kwargs):
        calls.append((smiles, kwargs["exhaustiveness"]))
        offset = {"CCO": 0.0, "CCN": -0.1, "CCC": -0.2, "CCCl": -0.3}[smiles]
        score = (-6.0 if kwargs["exhaustiveness"] == 4 else -8.0) + offset
        return {
            "smiles": smiles, "score": score, "valid": True, "status": "ok",
            "error": None, "artifacts": {"directory": "mock"},
        }

    candidates = [{"smiles": smi, "provider": "test"} for smi in ("CCO", "CCN", "CCC", "CCCl")]
    with patch("agents.evaluator.dock_smiles", side_effect=fake_dock):
        rows = evaluate_candidates(candidates, _config(), _target(), artifact_dir="unused")

    assert sum(exhaustiveness == 4 for _, exhaustiveness in calls) == 4
    assert sum(exhaustiveness == 16 for _, exhaustiveness in calls) == 2
    assert sum(row["evaluation_status"] == "complete" for row in rows) == 2
    assert sum(row["evaluation_status"] == "screened_out" for row in rows) == 2
    assert all((row["funnel"]["screening_dock"] or {}).get("valid") for row in rows)
    assert [row["smiles"] for row in rows] == ["CCO", "CCN", "CCC", "CCCl"]


def test_full_docking_cache_hit_is_always_promoted_by_funnel():
    cfg = _config()
    cfg["funnel"].update(full_dock_fraction=0.01, min_full_dock=1)
    cached = {"valid": True, "score": -9.0, "status": "ok", "artifacts": {"directory": "cache"}}

    def fake_dock(smiles, *_args, **kwargs):
        return {
            "smiles": smiles, "score": -6.0, "valid": True, "status": "ok",
            "error": None, "artifacts": {"directory": "mock"},
        }

    candidates = [{"smiles": smi} for smi in ("CCO", "CCN", "CCC")]
    with patch("agents.evaluator.dock_smiles", side_effect=fake_dock):
        rows = evaluate_candidates(
            candidates, cfg, _target(), dock_overrides={"CCO": cached}
        )
    cached_row = next(row for row in rows if row["smiles"] == "CCO")
    assert cached_row["evaluation_status"] == "complete"
    assert cached_row["dock"]["score"] == -9.0


def test_worker_count_is_bounded_by_jobs_and_cpu_budget(monkeypatch):
    monkeypatch.setattr("agents.evaluator.os.cpu_count", lambda: 8)
    assert _resolve_docking_workers({"workers": "auto", "cpu": 2}, 10) == 4
    assert _resolve_docking_workers({"workers": 8, "cpu": 2}, 3) == 3
    assert _resolve_docking_workers({"workers": 4, "cpu": 2}, 0) == 1


def test_worker_count_does_not_change_scientific_protocol_identity():
    first = _config()
    second = _config()
    second["vina"]["workers"] = 1
    second["funnel"]["fast_vina"]["workers"] = 2
    with patch("tools.dock_score.resolve_vina_binary", side_effect=FileNotFoundError):
        assert digest(evaluation_protocol(_target(), first, True)) == digest(
            evaluation_protocol(_target(), second, True)
        )


def test_failed_set_batches_gpu_refresh_and_disk_save_once(tmp_path):
    failed = FailedLigandSet(path=tmp_path / "failed.json")
    failed.enable_embeddings = True
    with (
        patch.object(failed, "_refresh_embeddings") as refresh,
        patch.object(failed, "_save") as save,
    ):
        assert failed.add_failed_many([("CCO", "x"), ("CCN", "y")]) == 2
    refresh.assert_called_once_with()
    save.assert_called_once_with()


def _write_summary(path: Path, improved: bool):
    path.mkdir(parents=True)
    (path / "summary.json").write_text(json.dumps({
        "agent_metrics": {"run_shows_improvement": improved}
    }), encoding="utf-8")


def test_futility_stops_when_required_success_rate_is_unreachable(tmp_path):
    runs = []
    outcomes = [True, False, False, False, False]
    for index, improved in enumerate(outcomes, 1):
        run_dir = tmp_path / f"run_{index}"
        _write_summary(run_dir, improved)
        runs.append({
            "group": "reflection_memory", "eligible": True,
            "run_dir": str(run_dir),
        })
    manifest = {
        "execution": {"repeats": 10},
        "confirmatory": {
            "treatment_group": "reflection_memory",
            "min_improved_run_rate": 0.7,
            "futility": {"enabled": True, "min_completed": 3},
        },
        "runs": runs,
    }
    result = assess_confirmatory_futility(manifest)
    assert result["stop"] is True
    assert result["maximum_possible_successes"] == 6
    assert result["minimum_successes"] == 7
    assert result["decision_final"] is True


def test_futility_does_not_stop_while_gate_remains_reachable(tmp_path):
    runs = []
    for index, improved in enumerate([True, False, False, False], 1):
        run_dir = tmp_path / f"run_{index}"
        _write_summary(run_dir, improved)
        runs.append({"group": "reflection_memory", "eligible": True, "run_dir": str(run_dir)})
    manifest = {
        "execution": {"repeats": 10},
        "confirmatory": {
            "treatment_group": "reflection_memory",
            "min_improved_run_rate": 0.7,
            "futility": {"enabled": True, "min_completed": 3},
        },
        "runs": runs,
    }
    assert assess_confirmatory_futility(manifest)["stop"] is False


def test_three_profiles_have_expected_fidelity_levels():
    document = yaml.safe_load(Path("experiments/profiles.yaml").read_text(encoding="utf-8"))
    profiles = document["profiles"]
    assert set(profiles) == {"smoke", "screening", "confirmatory"}
    assert profiles["smoke"]["mock"] is True
    assert profiles["smoke"]["dock_enabled"] is False
    assert profiles["screening"]["config_overrides"]["scoring"]["funnel"]["enabled"] is True
    assert profiles["confirmatory"]["config_overrides"]["scoring"]["funnel"]["enabled"] is False
    merged = deep_merge({"scoring": {"vina": {"seed": 2026}}}, profiles["screening"]["config_overrides"])
    assert merged["scoring"]["vina"]["seed"] == 2026
    assert merged["scoring"]["vina"]["workers"] == 4
