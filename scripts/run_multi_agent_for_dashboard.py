"""scripts/run_multi_agent_for_dashboard.py - Multi-agent worker for dashboard tasks.

Launched by agent_dashboard.py when a task has a multi_agent.flag file.
Reads the same config.yaml, runs run_multi_agent_loop() for one shot,
and writes the result to {task_dir}/multi_agent_log.json so the
dashboard can display it via its /api/tasks/<name> snapshot endpoint.

This worker does NOT touch the legacy Harness / TaskState schema; it
is fully additive. It picks up wherever it left off by reading the
multi_agent_log.json if present and appending a new round entry.

Usage:
    python scripts/run_multi_agent_for_dashboard.py \
        --task-dir runs/task_xxx \
        --config config.yaml
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description="Multi-agent dashboard worker.")
    parser.add_argument("--task-dir", required=True,
                        help="Dashboard task directory (must contain task.json)")
    parser.add_argument("--config", default="config.yaml",
                        help="Path to config.yaml (default: ./config.yaml)")
    parser.add_argument("--n-per-generator", type=int, default=2)
    parser.add_argument("--max-rounds", type=int, default=2)
    parser.add_argument("--mock", action="store_true",
                        help="Use mock providers (no real LLM call).")
    args = parser.parse_args()

    task_dir = Path(args.task_dir).resolve()
    if not (task_dir / "task.json").is_file():
        print(f"[multi_agent_worker] task.json missing in {task_dir}", file=sys.stderr)
        return 1
    if not (task_dir / "multi_agent.flag").is_file():
        print(f"[multi_agent_worker] multi_agent.flag missing in {task_dir}", file=sys.stderr)
        return 1

    import yaml
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = (ROOT / config_path).resolve()
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    from loop_multi_agent import (
        run_multi_agent_loop,
        multi_agent_enabled,
    )

    if not multi_agent_enabled(config.get("loop") or {}):
        print("[multi_agent_worker] loop.multi_agent.enabled=false; aborting.",
              file=sys.stderr)
        return 2

    # Output dir for run_multi_agent_loop is INSIDE the task directory so
    # the per-round JSON dumps land with the task.
    out_dir = task_dir / "multi_agent_run"
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[multi_agent_worker] task_dir={task_dir}")
    print(f"[multi_agent_worker] config={config_path}")
    print(f"[multi_agent_worker] n_per_generator={args.n_per_generator} "
          f"max_rounds={args.max_rounds} mock={args.mock}")

    started = time.time_ns()
    result = run_multi_agent_loop(
        config=config,
        output_dir=str(out_dir),
        n_per_generator=args.n_per_generator,
        max_rounds=args.max_rounds,
        use_mock=args.mock,
        verbose=True,
    )
    elapsed_ms = round((time.time_ns() - started) / 1e6, 1)

    # Load existing log if any (so we APPEND a new run entry rather than
    # overwriting prior runs of the same task).
    log_path = task_dir / "multi_agent_log.json"
    if log_path.is_file():
        try:
            log = json.loads(log_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            log = {"task_dir": str(task_dir), "runs": []}
    else:
        log = {"task_dir": str(task_dir), "runs": []}

    log["runs"].append({
        "started_ns": started,
        "elapsed_ms": elapsed_ms,
        "n_per_generator": args.n_per_generator,
        "max_rounds": args.max_rounds,
        "mock": args.mock,
        "result": result,
    })
    log["last_updated"] = time.time_ns() / 1e9
    log_path.write_text(json.dumps(log, indent=2, ensure_ascii=False),
                        encoding="utf-8")
    print(f"[multi_agent_worker] wrote {log_path}")
    print(f"[multi_agent_worker] done in {elapsed_ms} ms; "
          f"{len(result.get('rounds_log', []))} rounds logged.")
    return 0


if __name__ == "__main__":
    sys.exit(main())