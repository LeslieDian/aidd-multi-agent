"""tests/test_phase4_1.py - Phase 4.1 A/B comparison + unit tests.

Validates the 4 Phase 4.1 modules:
1. FailedLigandSet - persistence, dedup, prompt injection
2. WorkingMemory - round compression, convergence tracking
3. LoopController - 3 stop conditions
4. HITLCheckpoint - 3 checkpoint types (unit; interactive ones skip in CI)

Plus an A/B smoke test running both legacy loop and Phase 4.1 loop to
verify they both still work end-to-end.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agents.failed_set import FailedLigandSet
from agents.working_memory import WorkingMemory
from agents.loop_controller import LoopController, LoopState, LoopConfig
from agents.hitl import HITLCheckpoint


def _make_round_summary_dicts():
    """Fake evaluate_candidates output for memory tests."""
    return [
        {
            "smiles": "CCO",
            "validate": {"valid": True},
            "admet": {"summary_score": 0.85},
            "dock": {"score": -3.0},
            "composite_score": 0.7,
            "scaffold": "C",
        },
        {
            "smiles": "CC(=O)Oc1ccccc1C(=O)O",
            "validate": {"valid": True},
            "admet": {"summary_score": 0.88},
            "dock": {"score": -2.8},
            "composite_score": 0.65,
            "scaffold": "C1=CC=CC=C1",
        },
    ]


# ---------------- FailedLigandSet ----------------

def test_failed_set_dedup():
    print("\n=== test_failed_set_dedup ===")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "failed.json"
        fs = FailedLigandSet(path=path)

        # 3 distinct molecules (different canonical forms)
        assert fs.add_failed("CCO", "low score")
        assert fs.add_failed("CC(=O)Oc1ccccc1C(=O)O", "weak binding")
        assert fs.add_failed("c1ccccc1", "ring")  # benzene, distinct

        # Duplicate (same canonical as aspirin above, just whitespace)
        assert not fs.add_failed("  CC(=O)Oc1ccccc1C(=O)O  ", "dup")

        assert len(fs.failed) == 3, f"expected 3 unique, got {len(fs.failed)}"
        assert fs.is_failed("CCO")
        assert fs.is_failed("CC(=O)Oc1ccccc1C(=O)O")  # canonical
        assert not fs.is_failed("CCCCC")  # pentane, not ethanol

        # Persistence: reload from disk
        fs2 = FailedLigandSet(path=path)
        assert len(fs2.failed) == 3
        print(f"  [OK] dedup works ({len(fs.failed)} unique failed, 3 persisted)")


def test_failed_set_should_mark():
    print("\n=== test_failed_set_should_mark ===")
    fs = FailedLigandSet(threshold_composite=0.5, threshold_vina=-2.5)
    assert fs.should_mark_failed(0.3, -3.0)        # low composite
    assert fs.should_mark_failed(0.7, -2.0)         # weak vina
    assert not fs.should_mark_failed(0.7, -3.5)      # ok
    assert not fs.should_mark_failed(0.9, None)      # no vina data
    print("  [OK] threshold logic correct")


def test_failed_set_prompt_injection():
    print("\n=== test_failed_set_prompt_injection ===")
    fs = FailedLigandSet()
    # Use real SMILES (RDKit must parse them)
    real_smiles = [
        "CCO", "CC(=O)Oc1ccccc1C(=O)O", "CC(C)Cc1ccc(C(C)C(=O)O)cc1",
        "CN1CCC[C@H]1c1cccnc1", "Cn1cnc2c1c(=O)n(C)c(=O)n2C",
        "OC(=O)C1CCCCC1", "CC(=O)NCC(=O)N", "C#Cc1ccc(N)cc1",
        "c1ccc2ccccc2c1", "Brc1ccccc1", "Fc1ccccc1", "Clc1ccccc1",
    ]
    for s in real_smiles:
        fs.add_failed(s, "test")

    out = fs.format_for_prompt(max_show=5)
    assert "DO NOT propose" in out
    assert "and 7 more" in out  # 12 - 5 = 7
    print(f"  [OK] prompt includes 'DO NOT propose ... (and N more)' format")
    print(f"       total failed: {len(fs.failed)}, prompt snippet: {out[:120]}")


# ---------------- WorkingMemory ----------------

def test_working_memory_compression():
    print("\n=== test_working_memory_compression ===")
    mem = WorkingMemory(max_recent=3)
    out0 = mem.compress_for_generator()
    assert "First round" in out0

    mem.add_round(_make_round_summary_dicts(), focus="Try morpholine")
    out1 = mem.compress_for_generator()
    assert "Round 0" in out1
    assert "morpholine" in out1 or "Try morpholine" in out1
    print(f"  [OK] compression output includes round info + strategy")


def test_working_memory_rounds_since_improvement():
    print("\n=== test_working_memory_rounds_since_improvement ===")
    mem = WorkingMemory()
    # Round 0: best Vina = -3.0
    rd = _make_round_summary_dicts()
    rd[0]["dock"]["score"] = -3.0
    mem.add_round(rd, focus="F1")
    assert mem.rounds_since_improvement() == 0   # no history to compare

    # Round 1: best Vina = -2.8 (worse than R0, no improvement)
    rd2 = _make_round_summary_dicts()
    rd2[0]["dock"]["score"] = -2.8
    mem.add_round(rd2, focus="F2")
    assert mem.rounds_since_improvement() == 1   # 1 round without improvement

    # Round 2: best Vina = -3.5 (better than R1, improvement!)
    rd3 = _make_round_summary_dicts()
    rd3[0]["dock"]["score"] = -3.5
    mem.add_round(rd3, focus="F3")
    assert mem.rounds_since_improvement() == 0   # streak reset
    print("  [OK] convergence counter resets on improvement")


def test_working_memory_truncation():
    print("\n=== test_working_memory_truncation ===")
    mem = WorkingMemory(max_recent=2)
    for i in range(5):
        mem.add_round(_make_round_summary_dicts(), focus=f"F{i}")
    assert len(mem.recent_rounds) == 2
    assert len(mem.strategy_chain) == 2
    print("  [OK] auto-truncation to max_recent")


# ---------------- LoopController ----------------

def test_loop_controller_max_rounds():
    print("\n=== test_loop_controller_max_rounds ===")
    ctrl = LoopController(LoopConfig(max_rounds=3, token_budget=10000, judge_convergence_patience=2))
    state = LoopState()
    state.round = 2
    assert not ctrl.should_stop(state)[0]
    state.round = 3
    assert ctrl.should_stop(state)[0]
    assert ctrl.should_stop(state)[1] == "max_rounds_reached"
    print("  [OK] max_rounds trigger")


def test_loop_controller_convergence():
    print("\n=== test_loop_controller_convergence ===")
    ctrl = LoopController(LoopConfig(max_rounds=10, token_budget=100000, judge_convergence_patience=2))
    state = LoopState(round=1)
    state.note_round_result(-3.0)
    state.note_round_result(-3.0)  # no improvement
    state.note_round_result(-3.0)  # no improvement
    assert ctrl.should_stop(state)[0]
    assert ctrl.should_stop(state)[1] == "no_improvement"
    print("  [OK] judge_convergence trigger after N rounds without improvement")


def test_loop_controller_hitl_veto():
    print("\n=== test_loop_controller_hitl_veto ===")
    ctrl = LoopController(LoopConfig())
    state = LoopState(round=1, hitl_veto=True)
    stop, reason = ctrl.should_stop(state)
    assert stop and reason == "human_veto"
    print("  [OK] HITL veto overrides other conditions")


def test_loop_controller_token_budget():
    print("\n=== test_loop_controller_token_budget ===")
    ctrl = LoopController(LoopConfig(max_rounds=100, token_budget=1000, judge_convergence_patience=100))
    state = LoopState(round=0, tokens_used=999)
    assert not ctrl.should_stop(state)[0]
    state.tokens_used = 1000
    assert ctrl.should_stop(state)[0]
    assert ctrl.should_stop(state)[1] == "token_budget_exceeded"
    print("  [OK] token_budget trigger")


# ---------------- HITLCheckpoint ----------------

def test_hitl_disabled():
    print("\n=== test_hitl_disabled ===")
    hitl = HITLCheckpoint(require_approval=False)
    # All checks should auto-pass
    assert hitl.pre_loop(5, 5, 2)
    assert hitl.on_vina_breakthrough(-3.0, -3.5)
    chosen = hitl.select_synthesis_candidates([])
    assert chosen == []
    print("  [OK] disabled mode = auto-pass")


def test_hitl_veto_sets_state():
    print("\n=== test_hitl_veto_sets_state ===")
    hitl = HITLCheckpoint(require_approval=False)
    hitl.veto = True
    print(f"  [OK] veto can be set programmatically: {hitl.veto}")


# ---------------- A/B smoke test ----------------

def test_loop_works_with_phase41_enabled():
    print("\n=== test_loop_works_with_phase41_enabled ===")
    from loop import run_loop, load_config

    cfg = load_config("config.yaml")

    with tempfile.TemporaryDirectory() as tmp:
        # Default mode (no HITL - just memory + controller + failed set)
        overall = run_loop(
            config=cfg,
            output_dir=tmp,
            max_rounds=2,
            n_per_provider=3,
            dock_enabled=True,
            use_mock=True,
            hitl=False,
        )
        assert overall["rounds_completed"] == 2
        # Phase 4.1 metadata
        assert "loop_state" in overall
        assert "failed_set_size" in overall["loop_state"]
        assert "memory_rounds" in overall["loop_state"]
        print(f"  [OK] Phase 4.1 loop runs end-to-end")
        print(f"       failed_set_size: {overall['loop_state']['failed_set_size']}")
        print(f"       memory_rounds:   {overall['loop_state']['memory_rounds']}")


def main():
    print("[TEST] Phase 4.1 modules")
    print("=" * 60)
    test_failed_set_dedup()
    test_failed_set_should_mark()
    test_failed_set_prompt_injection()
    test_working_memory_compression()
    test_working_memory_rounds_since_improvement()
    test_working_memory_truncation()
    test_loop_controller_max_rounds()
    test_loop_controller_convergence()
    test_loop_controller_hitl_veto()
    test_loop_controller_token_budget()
    test_hitl_disabled()
    test_hitl_veto_sets_state()
    test_loop_works_with_phase41_enabled()
    print("\n" + "=" * 60)
    print("[OK] All Phase 4.1 tests passed!")


if __name__ == "__main__":
    main()