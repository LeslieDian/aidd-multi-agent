"""Regenerate JSON and Markdown reports for an existing benchmark folder."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from experiments.reporting import write_report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("benchmark_dir")
    args = parser.parse_args()
    json_path, md_path = write_report(args.benchmark_dir)
    print(json_path)
    print(md_path)


if __name__ == "__main__":
    main()
