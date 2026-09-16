"""Populate the protocol-scoped evaluation cache from completed run folders."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.evaluation_cache import EvaluationCache


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs", nargs="+", help="completed run directories")
    parser.add_argument("--cache-dir", default="memory/evaluation_cache")
    args = parser.parse_args()

    stored = 0
    existing = 0
    skipped = 0
    for run_arg in args.runs:
        run_dir = Path(run_arg)
        for round_path in sorted(run_dir.glob("round_*.json")):
            record = json.loads(round_path.read_text(encoding="utf-8"))
            protocol_id = record.get("protocol_id")
            if not protocol_id:
                skipped += len(record.get("candidates", []))
                continue
            cache = EvaluationCache(args.cache_dir, protocol_id)
            for candidate in record.get("candidates", []):
                result = cache.put(candidate)
                if result.get("stored"):
                    stored += 1
                elif result.get("reason") == "already_cached":
                    existing += 1
                else:
                    skipped += 1

    print(json.dumps({
        "stored": stored,
        "already_cached": existing,
        "skipped": skipped,
        "cache_dir": str(Path(args.cache_dir).resolve()),
    }, indent=2))


if __name__ == "__main__":
    main()
