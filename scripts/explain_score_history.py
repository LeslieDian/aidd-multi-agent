"""Explain saved scores without recomputation, model calls, or modifying source evidence."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agents.harness.attribution import breakdown, compare_breakdown


def explain(task_path, output):
    source = Path(task_path)
    destination = Path(output)
    if destination.exists():
        raise ValueError("Output exists; choose a new evidence file")
    task = json.loads(source.read_text(encoding="utf-8"))
    rows = []
    for cid, candidate in task["candidates"].items():
        parent = task["candidates"].get(candidate.get("parent_id"))
        row = {"candidate_id": cid, "smiles": candidate["smiles"],
               "property_attribution": breakdown(candidate, task["config"]["scoring"])}
        if parent:
            row["parent_id"] = candidate["parent_id"]
            row["attribution_delta"] = compare_breakdown(parent, candidate, task["config"]["scoring"])
        rows.append(row)
    result = {"source": str(source), "retrospective": True, "new_evaluations": 0, "model_calls": 0,
              "scope": "arithmetic reconciliation of saved scores, not a prospective prediction experiment", "candidates": rows}
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = explain(args.task, args.output)
    print(json.dumps({"new_evaluations": 0, "model_calls": 0, "verified": all(c["property_attribution"]["status"] == "verified" for c in result["candidates"])}))
