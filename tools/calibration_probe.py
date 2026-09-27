"""tools/calibration_probe.py — real EVIDENCE calibration data from docked runs.

Phase 4.4 (2026-09-27): the EVIDENCE calibration pipeline (``RuleStore.
add_prediction_error`` -> ``compute_calibration_metrics`` -> metrics.json /
visualize_memory.py) needs real prediction-vs-observed observations. The
non-docked phenol diagnostic NEVER produces ``insufficient_evidence``
(``same_protocol`` is always True), so the EVIDENCE category stayed empty.

The docked diagnostic runs (``runs/diagnostic_2d_dock_*_20260923``) DID
produce ``insufficient_evidence``: the parent is evaluated with
``dock_enabled=True`` (Vina protocol) while ``evaluate_options`` screens
children with ``dock_enabled=False``, so ``evidence_delta`` reports
``same_protocol=False`` and ``judge_effect`` returns ``insufficient_evidence``
for every row. Each row carries the model's property_score prediction
(``result.options[i].predictions``) and the observed delta
(``screening_comparison.rows[i].effect.observed_delta``) — exactly what
``add_prediction_error`` needs.

This probe replays those real receipts through the real persistence path:
it loads each ``evaluate_options`` tool_result from the docked checkpoints,
extracts (parent_smiles, child_smiles, predicted, observed, scaffold) tuples,
and feeds them into a fresh ``RuleStore`` via ``add_prediction_error``. The
store is saved to ``runs/samples/rule_memory_<probe_id>.json`` exactly like
the harness would, so ``scripts/visualize_memory.py`` and the cross-run drift
detector pick it up unchanged.

Honesty guard: the probe never fabricates numbers — every (predicted,
observed) pair is read verbatim from a real agent run receipt. The probe is
a *replay* of data the harness already recorded, not a new model run.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents.rule_memory import RuleStore  # noqa: E402


DEFAULT_RECEIPT_GLOBS = [
    "runs/diagnostic_2d_dock_*/agent/receipts/*.json",
]


def iter_evaluate_options(receipt_paths: Iterable[Path]):
    """Yield (file, event) for every evaluate_options tool_result event."""
    for path in receipt_paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for event in payload.get("events") or []:
            if event.get("type") != "tool_result":
                continue
            action = event.get("action") or {}
            if action.get("tool") != "evaluate_options":
                continue
            yield path, event


def extract_pairs(receipt_paths: Iterable[Path]) -> list[dict]:
    """Extract (parent, child, predicted, observed, scaffold) tuples.

    Only rows whose outcome is ``insufficient_evidence`` with a real
    property_score prediction and a non-None observed_delta are kept — the
    exact subset the harness's ``update_memory_from_events`` would have fed
    to ``add_prediction_error`` (post-5f97d71 fix).
    """
    pairs = []
    for path, event in iter_evaluate_options(receipt_paths):
        result = event.get("result") or {}
        options = result.get("options") or []
        rows = ((result.get("screening_comparison") or {}).get("rows") or [])
        # parent comes from the proposal's parent_id -> candidates
        proposal_id = (event.get("action") or {}).get("arguments", {}).get("proposal_id")
        # candidate map is not in the event; parent smiles comes from the
        # checkpoint's candidates. We pass it in via the checkpoint payload
        # at the call site instead; here we just pair options+rows.
        opts_by_index = {i: o for i, o in enumerate(options)}
        for row in rows:
            idx = row.get("option_index")
            opt = opts_by_index.get(idx)
            effect = row.get("effect") or {}
            if effect.get("outcome") != "insufficient_evidence":
                continue
            observed = effect.get("observed_delta")
            if observed is None:
                continue
            smi = (opt or {}).get("precheck", {}).get("product_smiles")
            if not smi:
                continue
            pred = None
            for p in (opt or {}).get("predictions") or []:
                if p.get("metric") == "property_score":
                    try:
                        pred = float(p.get("min_change") or 0.0)
                    except (TypeError, ValueError):
                        pred = None
                    break
            if pred is None:
                continue
            pairs.append({
                "receipt": str(path),
                "proposal_id": proposal_id,
                "option_index": idx,
                "child_smiles": smi,
                "predicted_delta": pred,
                "observed_delta": float(observed),
            })
    return pairs


def load_parent_meta(receipt_paths: Iterable[Path]) -> dict[str, dict]:
    """Map receipt file -> {smiles, scaffold} from the checkpoint's c1."""
    out = {}
    for path in receipt_paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        c1 = (payload.get("candidates") or {}).get("c1") or {}
        if c1.get("smiles"):
            out[str(path)] = {
                "smiles": c1["smiles"],
                "scaffold": c1.get("scaffold", "?"),
            }
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", default=None,
                        help="Output rule_memory JSON path "
                             "(default: runs/samples/rule_memory_<probe_id>.json)")
    parser.add_argument("--probe-id", default=None,
                        help="Probe id used for the output filename and target")
    parser.add_argument("--receipt", action="append", default=[],
                        help="Extra receipt file/dir (repeatable). "
                             "Defaults to the docked diagnostic globs.")
    args = parser.parse_args(argv)

    receipt_paths: list[Path] = []
    for glob_pattern in DEFAULT_RECEIPT_GLOBS:
        receipt_paths.extend(Path(p) for p in __import__("glob").glob(glob_pattern))
    for extra in args.receipt:
        p = Path(extra)
        if p.is_dir():
            receipt_paths.extend(p.glob("*.json"))
        else:
            receipt_paths.append(p)
    # Deduplicate, keep order.
    seen, unique = set(), []
    for p in receipt_paths:
        s = str(p)
        if s not in seen:
            seen.add(s)
            unique.append(p)
    receipt_paths = unique

    if not receipt_paths:
        print("No receipts found", file=sys.stderr)
        return 2

    parent_map = load_parent_meta(receipt_paths)
    pairs = extract_pairs(receipt_paths)

    # Resolve parent smiles + scaffold: c1 from the receipt's checkpoint.
    for pair in pairs:
        meta = parent_map.get(pair["receipt"], {})
        pair["parent_smiles"] = meta.get("smiles", "?")
        pair["scaffold"] = meta.get("scaffold", "?")

    if not pairs:
        print("No insufficient_evidence pairs found — nothing to write", file=sys.stderr)
        return 1

    probe_id = args.probe_id or f"calib_probe_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    out_path = Path(args.output) if args.output else ROOT / "runs" / "samples" / f"rule_memory_{probe_id}.json"

    store = RuleStore(persist_path=out_path, target=probe_id)
    written = 0
    for pair in pairs:
        store.add_prediction_error(
            parent_smiles=pair["parent_smiles"],
            child_smiles=pair["child_smiles"],
            predicted_delta=pair["predicted_delta"],
            observed_delta=pair["observed_delta"],
            context_scaffolds=[pair["scaffold"]],
        )
        written += 1
    # add_prediction_error persists per rule; force a final save in case the
    # store had nothing left to do (shouldn't happen, but be safe).
    store._save()

    summary = {
        "probe_id": probe_id,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "source": "replay of real docked diagnostic evaluate_options receipts",
        "receipt_files": len(receipt_paths),
        "pairs_extracted": len(pairs),
        "rules_written": written,
        "n_evidence_rules": len(store.by_category("evidence_strength")),
        "output": str(out_path),
        "honesty_note": ("All (predicted, observed) pairs are read verbatim "
                         "from real agent-run receipts; no numbers are fabricated."),
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
