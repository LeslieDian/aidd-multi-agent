"""Tests for scripts/visualize_memory.py (Phase 4.4 P3-4).

The script is a CLI; these tests cover the pure functions
(discovery, aggregation, JSON report) plus a CLI smoke test that
ensures matplotlib-missing mode degrades gracefully.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Make scripts/ importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from visualize_memory import (  # noqa: E402
    category_counts, discover_rule_memory_paths, evidence_pairs,
    load_rules, write_json_report,
)


def _make_rule(rule_id: str, category: str, *, predicted=None, observed=None,
                direction=None, evidence_strength=0.5, observations=1):
    pattern = {"parent_smiles": "Oc1ccccc1", "child_smiles": "COc1ccccc1"}
    if predicted is not None:
        pattern["predicted_delta"] = predicted
    if observed is not None:
        pattern["observed_delta"] = observed
    if direction is not None:
        pattern["direction"] = direction
        pattern["observations_log"] = [(predicted, observed, "2026-09-27T00:00:00Z")]
    return {
        "rule_id": rule_id,
        "category": category,
        "pattern": pattern,
        "evidence_strength": evidence_strength,
        "observations": observations,
    }


def test_category_counts():
    rules = [
        _make_rule("n1", "negative_constraint"),
        _make_rule("n2", "negative_constraint"),
        _make_rule("p1", "positive_transformation"),
        _make_rule("e1", "evidence_strength",
                    predicted=0.02, observed=0.005, direction="under"),
    ]
    counts = category_counts(rules)
    assert counts["negative_constraint"] == 2
    assert counts["positive_transformation"] == 1
    assert counts["evidence_strength"] == 1


def test_evidence_pairs_expand_log():
    """A single rule with 3 log entries -> 3 pairs."""
    rule = _make_rule("r", "evidence_strength", predicted=0.02, observed=0.005,
                       direction="under")
    rule["pattern"]["observations_log"] = [
        (0.020, 0.005, "t1"), (0.025, 0.012, "t2"), (0.018, 0.006, "t3"),
    ]
    pairs = evidence_pairs([rule])
    assert len(pairs) == 3
    assert all(p > o for p, o, *_ in pairs)  # all under-claims


def test_evidence_pairs_skip_non_evidence():
    rules = [
        _make_rule("p", "positive_transformation", predicted=0.02, observed=0.04),
        _make_rule("e", "evidence_strength", predicted=0.02, observed=0.005,
                    direction="under"),
    ]
    pairs = evidence_pairs(rules)
    assert len(pairs) == 1


def test_load_rules_round_trip(tmp_path):
    """A JSON file written by ``RuleStore._save`` can be reloaded."""
    path = tmp_path / "rules.json"
    payload = {
        "schema_version": 1,
        "target": "EGFR",
        "rules": {
            "n1": _make_rule("n1", "negative_constraint"),
            "e1": _make_rule("e1", "evidence_strength", predicted=0.02,
                              observed=0.005, direction="under"),
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    rules = load_rules([path])
    assert len(rules) == 2
    assert all("_source_file" in r for r in rules)


def test_write_json_report(tmp_path):
    rules = [
        _make_rule("n1", "negative_constraint", evidence_strength=1.0,
                    observations=3),
        _make_rule("e1", "evidence_strength", predicted=0.02, observed=0.005,
                    direction="under"),
    ]
    pairs = evidence_pairs(rules)
    counts = category_counts(rules)
    out = write_json_report(rules, pairs, counts, tmp_path)
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["n_rules_total"] == 2
    assert data["category_counts"]["negative_constraint"] == 1
    assert data["category_counts"]["evidence_strength"] == 1
    assert data["calibration"]["mean_abs_error"] == 0.015
    assert data["calibration"]["under_claim_count"] == 1
    assert data["calibration"]["under_claim_rate"] == 1.0
    # top_rules is sorted desc by evidence_strength, then observations
    assert data["top_rules"][0]["rule_id"] == "n1"


def test_discover_rule_memory_paths_finds_files(tmp_path):
    (tmp_path / "rule_memory_a.json").write_text("{}", encoding="utf-8")
    (tmp_path / "rule_memory_b.json").write_text("{}", encoding="utf-8")
    (tmp_path / "unrelated.json").write_text("{}", encoding="utf-8")
    found = discover_rule_memory_paths([tmp_path])
    names = sorted(p.name for p in found)
    assert names == ["rule_memory_a.json", "rule_memory_b.json"]


def test_cli_json_only_mode(tmp_path, capsys):
    """Running the CLI with --json-only must always succeed and write JSON.

    A missing --memory file produces a warning but the run still completes
    (so it can be used in CI / cron without manual file existence checks).
    """
    from visualize_memory import main
    rc = main([
        "--json-only",
        "--memory", str(tmp_path / "missing.json"),  # absent file, OK
        "--output", str(tmp_path),
    ])
    assert rc == 0
    # JSON written, but it's the empty-zero shape.
    report = json.loads((tmp_path / "memory_visualization.json").read_text(
        encoding="utf-8"))
    assert report["n_rules_total"] == 0
    assert report["calibration"]["mean_abs_error"] is None


def test_cli_no_inputs_exits_one(tmp_path, monkeypatch):
    """No --memory and no ./runs or ./memory in cwd -> rc=1 with helpful msg."""
    from visualize_memory import main
    monkeypatch.chdir(tmp_path)  # cwd has no runs/ or memory/
    rc = main(["--json-only", "--output", str(tmp_path)])
    assert rc == 1


def test_cli_with_real_rule_file(tmp_path):
    """End-to-end CLI: a populated rule memory file -> JSON + PNG outputs."""
    from visualize_memory import main
    rule_file = tmp_path / "rules.json"
    payload = {
        "schema_version": 1,
        "target": "EGFR",
        "rules": {
            "n1": _make_rule("n1", "negative_constraint"),
            "e1": _make_rule("e1", "evidence_strength", predicted=0.02,
                              observed=0.005, direction="under"),
        },
    }
    rule_file.write_text(json.dumps(payload), encoding="utf-8")
    out_dir = tmp_path / "figs"
    rc = main(["--memory", str(rule_file), "--output", str(out_dir)])
    assert rc == 0
    # JSON always written
    assert (out_dir / "memory_visualization.json").exists()
    report = json.loads((out_dir / "memory_visualization.json").read_text(
        encoding="utf-8"))
    assert report["n_rules_total"] == 2
    # PNGs only when matplotlib is present
    try:
        import matplotlib  # noqa: F401
        assert (out_dir / "memory_categories_pie.png").exists()
    except ImportError:
        pass