"""Run a controlled AIDD experiment matrix and produce statistical reports."""
from __future__ import annotations

import argparse
import copy
import json
import math
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from experiments.reporting import write_report, extract_run_metrics
from experiments.contract import treatments, validate_design, resolve_budget, check_resume, normalized_config, contract_hashes
from loop import load_config, run_loop


def deep_merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def assess_run_quality(
    run_dir: Path,
    expected_rounds: int,
    expected_providers: list[str],
    n_per_provider: int,
    judge_enabled: bool,
) -> dict:
    """Decide whether a run is eligible for budget-matched statistics."""
    reasons = []
    summary_path = run_dir / "summary.json"
    if not summary_path.exists():
        return {"eligible": False, "reasons": ["missing_summary"]}
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if summary.get("status") != "finished":
        reasons.append(f"run_status={summary.get('status')}")
    if summary.get("rounds_completed") != expected_rounds:
        reasons.append(
            f"rounds_completed={summary.get('rounds_completed')} expected={expected_rounds}"
        )

    for round_number in range(expected_rounds):
        proposal_path = run_dir / f"proposals_{round_number}.json"
        round_path = run_dir / f"round_{round_number}.json"
        if not proposal_path.exists() or not round_path.exists():
            reasons.append(f"round_{round_number}:missing_files")
            continue
        outputs = json.loads(proposal_path.read_text(encoding="utf-8"))
        by_provider = {output.get("provider"): output for output in outputs}
        for provider in expected_providers:
            output = by_provider.get(provider)
            if output is None:
                reasons.append(f"round_{round_number}:{provider}:missing")
                continue
            if output.get("error"):
                reasons.append(f"round_{round_number}:{provider}:error")
            actual = len(output.get("smiles_list") or [])
            if actual != n_per_provider:
                reasons.append(
                    f"round_{round_number}:{provider}:candidates={actual} expected={n_per_provider}"
                )
        if judge_enabled:
            record = json.loads(round_path.read_text(encoding="utf-8"))
            if (record.get("judgment") or {}).get("status") != "ok":
                reasons.append(f"round_{round_number}:judge_not_ok")
    return {"eligible": not reasons, "reasons": reasons}


def assess_confirmatory_futility(manifest: dict) -> dict:
    """Return a deterministic stop when the required success rate is unreachable."""
    spec = manifest.get("confirmatory") or {}
    futility = spec.get("futility") or {}
    if not spec or not bool(futility.get("enabled", False)):
        return {"stop": False, "reason": "disabled"}

    arms = treatments(spec)
    if len(arms) > 1:
        decisions = {}
        for arm in arms:
            child = copy.deepcopy(manifest)
            child['confirmatory'].pop('treatment_groups', None)
            child['confirmatory']['treatment_group'] = arm
            decisions[arm] = assess_confirmatory_futility(child)
        stop = all(d['stop'] for d in decisions.values())
        return {'stop': stop, 'decision_final': stop, 'treatments': decisions,
                'reason': 'all_treatments_unreachable' if stop else 'at_least_one_reachable'}
    treatment = arms[0]
    planned = int(manifest["execution"]["repeats"])
    minimum_rate = float(spec.get("min_improved_run_rate", 0.7))
    minimum_successes = math.ceil(minimum_rate * planned - 1e-12)
    completed = 0
    successes = 0
    seen = set()
    for run in manifest.get("runs", []):
        if run.get("group") != treatment or not run.get("eligible"):
            continue
        summary_path = Path(run["run_dir"]) / "summary.json"
        if not summary_path.is_file():
            continue
        repeat_key = run.get('repeat') if run.get('repeat') is not None else run['run_dir']
        if repeat_key in seen:
            continue
        seen.add(repeat_key)
        metric = spec.get('improvement_metric', 'run_improvement_rate')
        if metric == 'safe_run_improvement_rate':
            value = extract_run_metrics(summary_path.parent).get(metric)
            improved = value == 1 if value is not None else False
        else:
            summary = json.loads(summary_path.read_text(encoding='utf-8'))
            improved = (summary.get('agent_metrics') or {}).get('run_shows_improvement')
        # Missing evidence cannot establish success and still consumes a completed repeat.
        improved = improved is True
        if isinstance(improved, bool):
            completed += 1
            successes += int(improved)

    remaining = max(0, planned - completed)
    maximum_successes = successes + remaining
    min_completed = max(1, int(futility.get("min_completed", 3)))
    impossible = completed >= min_completed and maximum_successes < minimum_successes
    return {
        "stop": impossible,
        "reason": "minimum_improvement_rate_unreachable" if impossible else "still_reachable",
        "treatment_group": treatment,
        "completed": completed,
        "successes": successes,
        "remaining": remaining,
        "minimum_successes": minimum_successes,
        "maximum_possible_successes": maximum_successes,
        "minimum_improved_run_rate": minimum_rate,
        "decision_final": impossible,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", default="experiments/matrix.yaml")
    parser.add_argument("--profiles", default="experiments/profiles.yaml")
    parser.add_argument("--profile", choices=("smoke", "screening", "confirmatory"))
    parser.add_argument("--output", default="benchmarks")
    parser.add_argument("--benchmark-id")
    parser.add_argument("--groups", nargs="+")
    parser.add_argument("--repeats", type=int)
    parser.add_argument("--rounds", type=int)
    parser.add_argument("--n", type=int)
    parser.add_argument("--mock", action="store_true")
    parser.add_argument("--no-dock", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--resume", action="store_true",
                        help="continue an existing benchmark and skip eligible repeats")
    parser.add_argument("--max-attempts", type=int, default=3,
                        help="maximum attempts for each eligible repeat")
    parser.add_argument("--no-futility-stop", action="store_true",
                        help="finish the fixed sample even after a deterministic gate failure")
    parser.add_argument('--dry-run', action='store_true', help='Validate and display the resolved plan; no writes or API calls')
    args = parser.parse_args()

    matrix_path = Path(args.matrix).resolve()
    matrix = yaml.safe_load(matrix_path.read_text(encoding="utf-8"))
    base_path = (matrix_path.parent / matrix["base_config"]).resolve()
    base_config = load_config(str(base_path))
    execution = matrix.get("execution") or {}
    profile_name = args.profile or execution.get("profile")
    profile = {}
    if profile_name:
        profiles_path = Path(args.profiles).resolve()
        profiles_doc = yaml.safe_load(profiles_path.read_text(encoding="utf-8")) or {}
        profile = (profiles_doc.get("profiles") or {}).get(profile_name) or {}
        if not profile:
            raise ValueError(f"unknown or empty experiment profile: {profile_name}")
        base_config = deep_merge(base_config, profile.get("config_overrides") or {})
    budget = resolve_budget({'repeats': args.repeats, 'rounds': args.rounds,
                             'candidates_per_round_per_generator': args.n},
                            execution, profile, smoke=profile_name == 'smoke')
    repeats, rounds, n_per_provider = (budget[k] for k in
        ('repeats', 'rounds', 'candidates_per_round_per_generator'))
    use_mock = bool(args.mock or profile.get("mock", False))
    dock_enabled = bool(profile.get("dock_enabled", True)) and not args.no_dock
    if min(repeats, rounds, n_per_provider, args.max_attempts) < 1:
        raise ValueError("repeats, rounds, n, and max-attempts must be positive")

    selected = args.groups or list(matrix["groups"])
    unknown = sorted(set(selected) - set(matrix["groups"]))
    if unknown:
        raise ValueError(f"unknown groups: {unknown}")
    validate_design(matrix, selected)
    benchmark_id = args.benchmark_id or (
        datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    )
    root = Path(args.output) / benchmark_id
    if root.exists() and not args.resume:
        raise FileExistsError(f"benchmark output already exists: {root}")
    manifest_path = root / "benchmark_manifest.json"
    if args.resume:
        if not manifest_path.exists():
            raise FileNotFoundError(f"cannot resume without manifest: {manifest_path}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        prior = manifest["execution"]
        expected = (int(prior["repeats"]), int(prior["rounds"]),
                    int(prior["candidates_per_round_per_generator"]))
        if (repeats, rounds, n_per_provider) != expected:
            raise ValueError(f"resume budget mismatch: requested={(repeats, rounds, n_per_provider)} existing={expected}")
        if manifest.get("mode") != {"mock": use_mock, "dock_enabled": dock_enabled}:
            raise ValueError("resume mode differs from the existing benchmark")
        if manifest["execution"].get("profile") != profile_name:
            raise ValueError("resume profile differs from the existing benchmark")
        selected = list(manifest["groups"])
        configs = {g: deep_merge(base_config, matrix['groups'][g].get('overrides') or {}) for g in selected}
        checked = check_resume(ROOT, manifest, matrix, configs)
        print('[BENCH] resume checks: ' + json.dumps(checked))
        manifest.setdefault('resume_history', []).append({
            'at': datetime.now().isoformat(timespec='seconds'), **checked,
            'old_max_attempts': prior.get('max_attempts_per_repeat'), 'new_max_attempts': args.max_attempts})
        manifest['execution']['max_attempts_per_repeat'] = args.max_attempts
        manifest.pop("finished_at", None)
    else:
        manifest = {
            "contract_hashes": contract_hashes(ROOT),
            "resolved_group_configs": {g: normalized_config(deep_merge(base_config, matrix['groups'][g].get('overrides') or {})) for g in selected},
            "schema_version": 1,
            "benchmark_id": benchmark_id,
            "name": matrix.get("name"),
            "description": matrix.get("description"),
            "matrix_path": str(matrix_path),
            "base_config": str(base_path),
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "mode": {"mock": use_mock, "dock_enabled": dock_enabled},
            "execution": {
                "repeats": repeats,
                "rounds": rounds,
                "candidates_per_round_per_generator": n_per_provider,
                "primary_metric": execution.get("primary_metric", "best_composite_global"),
                "max_attempts_per_repeat": args.max_attempts,
                "order": execution.get("order", "grouped"),
                "profile": profile_name,
            },
            "baseline_group": matrix.get("baseline_group", "baseline"),
            "confirmatory": matrix.get("confirmatory"),
            "groups": {name: matrix["groups"][name] for name in selected},
            "runs": [],
        }
    print('[BENCH] resolved plan: ' + json.dumps({
        'groups': selected, 'budget': budget, 'profile': profile_name,
        'mode': manifest['mode'], 'primary_metric': manifest['execution']['primary_metric'],
        'confirmatory': manifest.get('confirmatory'), 'max_attempts': args.max_attempts}, ensure_ascii=False))
    if args.dry_run:
        return
    root.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    order = manifest["execution"].get("order", "grouped")
    if order not in {"grouped", "interleaved"}:
        raise ValueError("execution.order must be grouped or interleaved")
    schedule = (
        [(group, repeat) for repeat in range(1, repeats + 1) for group in selected]
        if order == "interleaved"
        else [(group, repeat) for group in selected for repeat in range(1, repeats + 1)]
    )

    if (
        args.resume and not args.no_futility_stop
        and (manifest.get("early_stop") or {}).get("decision_final")
    ):
        json_report, md_report = write_report(root)
        print(f"[BENCH] resume skipped: final futility stop already recorded")
        print(f"[BENCH] report JSON: {json_report}")
        print(f"[BENCH] report Markdown: {md_report}")
        return

    stop_benchmark = False
    for group, repeat in schedule:
        group_spec = matrix["groups"][group]
        repeat_dir = root / group / f"repeat_{repeat:02d}"
        existing_attempts = []
        if repeat_dir.exists():
            known_dirs = {run.get("run_dir") for run in manifest["runs"]}
            for path in sorted(repeat_dir.glob("attempt_*")):
                try:
                    existing_attempts.append(int(path.name.split("_")[-1]))
                except ValueError:
                    continue
                meta_path = path / "benchmark_run.json"
                if args.resume and not meta_path.exists():
                    orphan_meta = {
                        "benchmark_id": benchmark_id,
                        "group": group,
                        "repeat": repeat,
                        "attempt": existing_attempts[-1],
                        "started_at": None,
                        "finished_at": datetime.now().isoformat(timespec="seconds"),
                        "duration_seconds": None,
                        "status": "interrupted",
                        "eligible": False,
                        "quality_reasons": ["incomplete_attempt_found_on_resume"],
                        "run_id": None,
                        "error": None,
                    }
                    meta_path.write_text(
                        json.dumps(orphan_meta, indent=2, ensure_ascii=False), encoding="utf-8"
                    )
                    resolved = str(path.resolve())
                    if resolved not in known_dirs:
                        manifest["runs"].append(orphan_meta | {"run_dir": resolved})
                        known_dirs.add(resolved)
        completed = any(
            run.get("group") == group and run.get("repeat") == repeat
            and run.get("eligible")
            for run in manifest["runs"]
        )
        if completed:
            print(f"[BENCH] resume: skip eligible group={group} repeat={repeat}")
            continue
        first_attempt = max(existing_attempts, default=0) + 1
        eligible = False
        for attempt in range(first_attempt, args.max_attempts + 1):
            run_dir = (
                root / group / f"repeat_{repeat:02d}" / f"attempt_{attempt:02d}"
            )
            config = deep_merge(base_config, group_spec.get("overrides") or {})
            config.setdefault("loop", {}).update({
                "require_all_generators": True,
                "generation_max_attempts": 3,
                "judge_max_attempts": 3,
            })
            config.setdefault("loop", {})["memory_namespace"] = (
                f"bench_{benchmark_id}_{group}_r{repeat:02d}_a{attempt:02d}"
            )
            run_dir.mkdir(parents=True)
            (run_dir / "resolved_config.yaml").write_text(
                yaml.safe_dump(config, sort_keys=False, allow_unicode=True),
                encoding="utf-8",
            )
            print(
                f"\n[BENCH] group={group} repeat={repeat}/{repeats} "
                f"attempt={attempt}/{args.max_attempts}"
            )
            started_at = datetime.now().isoformat(timespec="seconds")
            start = time.perf_counter()
            error = None
            try:
                result = run_loop(
                    config=config,
                    output_dir=str(run_dir),
                    max_rounds=rounds,
                    n_per_provider=n_per_provider,
                    dock_enabled=dock_enabled,
                    use_mock=use_mock,
                    verbose=not args.quiet,
                )
                status = result.get("status")
                run_id = result.get("run_id")
            except Exception as exc:
                status = "error"
                run_id = None
                error = f"{type(exc).__name__}: {exc}"
            duration = round(time.perf_counter() - start, 3)
            quality = (
                assess_run_quality(
                    run_dir, rounds, base_config["llm"].get("generators", []),
                    n_per_provider, bool(config["loop"].get("judge_enabled", True)),
                )
                if error is None else {"eligible": False, "reasons": [error]}
            )
            eligible = bool(quality["eligible"])
            run_meta = {
                "benchmark_id": benchmark_id,
                "group": group,
                "repeat": repeat,
                "attempt": attempt,
                "started_at": started_at,
                "finished_at": datetime.now().isoformat(timespec="seconds"),
                "duration_seconds": duration,
                "status": status,
                "eligible": eligible,
                "quality_reasons": quality["reasons"],
                "run_id": run_id,
                "error": error,
            }
            (run_dir / "benchmark_run.json").write_text(
                json.dumps(run_meta, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            manifest["runs"].append(run_meta | {"run_dir": str(run_dir.resolve())})
            manifest_path.write_text(
                json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            if eligible:
                print(f"[BENCH] eligible repeat completed in {duration:.1f}s")
                if not args.no_futility_stop:
                    futility = assess_confirmatory_futility(manifest)
                    if futility.get("stop"):
                        manifest["early_stop"] = {
                            **futility,
                            "stopped_at": datetime.now().isoformat(timespec="seconds"),
                        }
                        manifest_path.write_text(
                            json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
                        )
                        print(
                            "[BENCH] futility stop: required improvement rate "
                            "is mathematically unreachable"
                        )
                        stop_benchmark = True
                break
            print(f"[BENCH] excluded attempt: {quality['reasons']}")
        if not eligible:
            print(f"[BENCH] no eligible result for group={group} repeat={repeat}")
        if stop_benchmark:
            break

    manifest["finished_at"] = datetime.now().isoformat(timespec="seconds")
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    json_report, md_report = write_report(root)
    print(f"\n[BENCH] report JSON: {json_report}")
    print(f"[BENCH] report Markdown: {md_report}")


if __name__ == "__main__":
    main()
