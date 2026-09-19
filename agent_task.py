"""CLI for persistent AIDD tasks. The existing loop.py remains the benchmark entry."""
import argparse
import json
from pathlib import Path

import yaml

from agents.harness import CheckpointStore, Harness, TaskState
from agents.harness.molecule_ops import add_seed_candidates, normalize_constraints


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start")
    start.add_argument("--goal", required=True)
    start.add_argument("--config", default="config.yaml")
    start.add_argument("--mock", action="store_true")
    start.add_argument("--dock", action="store_true", help="Enable real Vina; default is property screening")
    start.add_argument("--max-steps", type=int, default=20)
    start.add_argument("--max-model-calls", type=int, default=40)
    start.add_argument("--max-evaluations", type=int, default=100)
    start.add_argument("--seed-smiles", action="append", default=[],
                       help="User-supplied parent SMILES; repeat for multiple parents")
    start.add_argument("--constraints-json", default="{}",
                       help="Structured constraint object as JSON")
    resume = commands.add_parser("resume")
    resume.add_argument("--instruction")
    resume.add_argument("--ack-interrupted", action="store_true",
                        help="Acknowledge uncertain interrupted tool outcome; never automatically replay it")
    status = commands.add_parser("status")
    pause = commands.add_parser("pause", help="Request pause at the next execution boundary")
    cancel = commands.add_parser("cancel", help="Request permanent cancellation at the next execution boundary")
    steer = commands.add_parser("steer", help="Submit new requirements to a running task")
    steer.add_argument("--instruction", required=True)
    constraints = commands.add_parser("constraints", help="Update enforceable task constraints")
    constraints.add_argument("--constraints-json", required=True)
    for command in (start, resume, status, pause, cancel, steer, constraints):
        command.add_argument("--task-dir", required=True)
    for command in (start, resume):
        command.add_argument("--steps", type=int, default=5, help="Pause after this many decisions")
    args = parser.parse_args(argv)
    store = CheckpointStore(args.task_dir)
    try:
        if args.command == "start":
            if min(args.max_steps, args.steps, args.max_model_calls, args.max_evaluations) < 1 or not args.goal.strip():
                raise ValueError("Goal must be nonempty and step limits positive")
            config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
            # Resolve receptor paths once, so recovery is independent of working directory.
            receptor = config["target"]["receptor_pdbqt"]
            config["target"]["receptor_pdbqt"] = str(Path(receptor).resolve())
            raw_constraints = json.loads(args.constraints_json)
            if args.seed_smiles and "require_verified_refinement" not in raw_constraints:
                raw_constraints["require_verified_refinement"] = True
            if args.seed_smiles:
                raw_constraints.setdefault("require_meaningful_improvement", True)
                raw_constraints.setdefault("require_planned_edits", True)
                raw_constraints.setdefault("require_option_screening", True)
            task_constraints = normalize_constraints(raw_constraints, dock_enabled=args.dock)
            state = TaskState(goal=args.goal, config=config, mock=args.mock,
                              dock_enabled=args.dock, max_steps=args.max_steps,
                              max_model_calls=args.max_model_calls, max_evaluations=args.max_evaluations,
                              constraints=task_constraints)
            if args.seed_smiles:
                add_seed_candidates(state, args.seed_smiles, source="cli")
            with store.lock():
                if store.path.exists():
                    raise ValueError("Task directory already contains a checkpoint")
                store.save(state)
        if args.command in {"pause", "cancel", "steer", "constraints"}:
            changes = json.loads(args.constraints_json) if args.command == "constraints" else None
            if changes is not None:
                # Validate before queueing so a bad request cannot poison the control inbox.
                current = store.load()
                normalize_constraints({**normalize_constraints(current.constraints, dock_enabled=current.dock_enabled), **changes})
            control = store.submit_control(args.command, getattr(args, "instruction", None), changes)
            updated = Harness(store).apply_controls()
            applied = updated and control["id"] in updated.processed_controls
            print(json.dumps({"status": "applied" if applied else "queued", "control": control}, ensure_ascii=False))
            return 0
        if args.command == "status":
            state = store.load()
        else:
            state = Harness(store, on_event=lambda event: print(json.dumps(event, ensure_ascii=False))).run(
                max_actions=args.steps, instruction=getattr(args, "instruction", None),
                acknowledge_interrupted=getattr(args, "ack_interrupted", False))
        print(json.dumps({"task_id": state.task_id, "goal": state.goal,
                          "status": state.status, "reason": state.reason,
                          "revision": state.revision, "steps_used": state.steps_used,
                          "max_steps": state.max_steps, "candidates": len(state.candidates),
                          "model_calls_used": state.model_calls_used, "max_model_calls": state.max_model_calls,
                          "evaluations_used": state.evaluations_used, "max_evaluations": state.max_evaluations,
                          "instructions": state.instructions, "pending": state.pending,
                          "constraints": state.constraints, "constraint_history": state.constraint_history,
                          "checkpoint": str(store.path), "final": state.final}, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, RuntimeError, OSError) as exc:
        parser.exit(2, f"Task error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
