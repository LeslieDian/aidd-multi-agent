"""Warm the docking-only cache from completed run or benchmark directories."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.docking_cache import DockingCache
from tools.provenance import digest, docking_protocol_from_evaluation


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", help="run or benchmark directories to scan")
    parser.add_argument("--cache-dir", default="memory/docking_cache")
    args = parser.parse_args()

    caches: dict[str, DockingCache] = {}
    seen: set[tuple[str, str]] = set()
    scanned = stored = existing = incompatible = 0
    for root_arg in args.paths:
        root = Path(root_arg)
        for round_path in root.rglob("round_*.json"):
            record = json.loads(round_path.read_text(encoding="utf-8"))
            for candidate in record.get("candidates") or []:
                scanned += 1
                provenance = candidate.get("provenance") or {}
                dock = candidate.get("dock") or {}
                smiles = candidate.get("smiles", "")
                if not provenance or not dock.get("valid") or not smiles:
                    incompatible += 1
                    continue
                protocol_id = digest(docking_protocol_from_evaluation(provenance))
                cache = caches.setdefault(
                    protocol_id, DockingCache(args.cache_dir, protocol_id, enabled=True)
                )
                canonical = cache.canonicalize(smiles)
                identity = (protocol_id, canonical or "")
                if not canonical or identity in seen:
                    continue
                seen.add(identity)
                result = cache.put(smiles, dock, source={
                    "run_id": candidate.get("run_id"),
                    "candidate_id": candidate.get("candidate_id"),
                    "round": candidate.get("round"),
                    "source_file": str(round_path.resolve()),
                })
                if result.get("stored"):
                    stored += 1
                elif result.get("reason") == "already_cached":
                    existing += 1

    print(json.dumps({
        "scanned_candidates": scanned,
        "unique_compatible_dockings": len(seen),
        "stored": stored,
        "already_cached": existing,
        "incompatible_or_invalid": incompatible,
        "protocols": len(caches),
        "cache_dir": str(Path(args.cache_dir).resolve()),
    }, indent=2))


if __name__ == "__main__":
    main()
