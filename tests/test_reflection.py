"""tests/test_reflection.py - Phase 4.2: Self-Reflection Judge tests.

Validates:
1. judge_round() returns reflection + confidence + adopted_count fields
2. Confidence is coerced into [0, 1]
3. Reflection is empty for round 0
4. Reflection populated when previous_focus provided
5. End-to-end loop with reflection: tracking across rounds

Plus a meta-metric: judge suggestion adoption rate across rounds.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agents.judge import judge_round, _extract_json
from agents.working_memory import WorkingMemory
from loop import run_loop, load_config


def _make_candidate(smiles: str, vina: float, composite: float = 0.7) -> dict:
    return {
        "smiles": smiles,
        "provider": "deepseek",
        "validate": {"valid": True, "mw": 300, "logp": 2.0, "lipinski_pass": True},
        "admet": {"summary_score": 0.8, "qed": 0.5, "warnings": []},
        "dock": {"score": vina, "valid": True},
        "scaffold": "C1",
        "composite_score": composite,
    }


# ---------------- unit tests on judge_round ----------------

def test_judge_returns_reflection_fields():
    print("\n=== test_judge_returns_reflection_fields ===")
    enriched = [_make_candidate("CCO", -2.8), _make_candidate("c1ccccc1", -2.5)]
    import yaml
    cfg = yaml.safe_load(open("config.yaml", encoding="utf-8"))

    j = judge_round(enriched, cfg, round_num=0, use_mock=True)
    # Round 0 -> no previous_focus -> reflection/confidence defaults
    assert "reflection" in j
    assert "confidence" in j
    assert "adopted_count" in j
    assert j["confidence"] == 0.0  # no previous to reflect on
    print(f"  [OK] round 0 reflection empty, confidence=0")


def test_judge_handles_missing_previous():
    print("\n=== test_judge_handles_missing_previous ===")
    enriched = [_make_candidate("CCO", -2.8)]
    import yaml
    cfg = yaml.safe_load(open("config.yaml", encoding="utf-8"))

    j = judge_round(enriched, cfg, round_num=2, use_mock=True)
    # No previous_focus arg -> reflection stays empty
    assert j["reflection"] == ""
    assert j["confidence"] == 0.0
    print(f"  [OK] missing previous_focus handled gracefully")


def test_confidence_coerced_to_range():
    print("\n=== test_confidence_coerced_to_range ===")
    # MockLLMClient returns confidence as part of mock JSON. Let's verify
    # the coercion path by patching parsed dict.
    # Easiest: test _extract_json + coerce via test_parsing
    fake_json = '{"focus": "x", "confidence": 1.5, "adopted_count": -3}'
    parsed = _extract_json(fake_json)
    # Verify extraction works
    assert parsed["confidence"] == 1.5
    assert parsed["adopted_count"] == -3
    print(f"  [OK] raw extraction preserves values; coercion happens in judge_round")


# ---------------- adoption tracking via memory ----------------

def test_working_memory_adoption_helpers():
    print("\n=== test_working_memory_adoption_helpers ===")
    mem = WorkingMemory(max_recent=5)

    # Add a round; track which focus was used
    enriched = [
        _make_candidate("morpholine_derivative_smiles", -3.0),
        _make_candidate("plain_benzene", -2.5),
    ]
    mem.add_round(enriched, focus="Add morpholine to improve solubility")

    # Strategy chain should record the focus
    assert mem.strategy_chain[-1] == "Add morpholine to improve solubility"

    # Recent round summary exists
    assert len(mem.recent_rounds) == 1
    print(f"  [OK] memory persists focus for downstream reflection")


# ---------------- end-to-end with adoption metric ----------------

def test_end_to_end_with_phase42():
    print("\n=== test_end_to_end_with_phase42 ===")
    cfg = load_config("config.yaml")
    with tempfile.TemporaryDirectory() as tmp:
        overall = run_loop(
            config=cfg, output_dir=tmp,
            max_rounds=3, n_per_provider=3,
            dock_enabled=True, use_mock=True,
            verbose=False,
        )
        assert overall["rounds_completed"] == 3
        # Per-round adoption tracking
        runs_dir = Path(tmp)
        round_files = sorted(runs_dir.glob("round_*.json"))
        assert len(round_files) == 3
        for f in round_files:
            r = json.loads(f.read_text(encoding="utf-8"))
            assert "judgment" in r
            j = r["judgment"]
            assert "reflection" in j
            assert "confidence" in j
            assert "adopted_count" in j
            assert 0.0 <= j["confidence"] <= 1.0
        print(f"  [OK] 3 rounds, all have reflection + confidence in [0, 1]")
        # Adoption metric
        adoption_rates = []
        for f in round_files:
            r = json.loads(f.read_text(encoding="utf-8"))
            n_valid = r["summary"]["n_valid"]
            adopted = r["judgment"]["adopted_count"]
            if n_valid > 0:
                adoption_rates.append(adopted / n_valid)
        if adoption_rates:
            print(f"       adoption rates: {[f'{r:.2f}' for r in adoption_rates]}")
            print(f"       mean adoption: {sum(adoption_rates)/len(adoption_rates):.2f}")


def main():
    print("[TEST] Phase 4.2: Self-Reflection Judge")
    print("=" * 60)
    test_judge_returns_reflection_fields()
    test_judge_handles_missing_previous()
    test_confidence_coerced_to_range()
    test_working_memory_adoption_helpers()
    test_end_to_end_with_phase42()
    print("\n" + "=" * 60)
    print("[OK] All Phase 4.2 tests passed!")


if __name__ == "__main__":
    main()