"""Tests for the safety-gated best_safe_vina curve (Phase 4.4 follow-up, P1).

REVIEW_MINIMAX_ADVICE_20260917 item 1: "让'首末轮变化'也能按安全口径给出" —
compute_agent_metrics must surface the safety-gated Vina curve and its
first-to-last delta, not just the all-candidates best_vina curve.
"""
from __future__ import annotations

import json

import pytest

from agents.agent_metrics import compute_agent_metrics


def _summary(round_no, best_vina, best_safe_vina=None, **extra):
    base = {
        "round": round_no,
        "n_total": 4, "n_valid": 3, "n_complete": 3,
        "valid_ratio": 0.75, "n_unique_scaffolds": 2, "avg_admet": 0.7,
        "best_vina": best_vina,
        "best_smiles": "X",
        "best_safe_vina": best_safe_vina,
        "best_safe_smiles": None,
    }
    base.update(extra)
    return base


def test_safe_vina_curve_and_delta_populated():
    """best_safe_vina_curve mirrors summarize_round's per-round best_safe_vina;
    best_safe_vina_delta is first-to-last on that curve."""
    summaries = [
        _summary(0, best_vina=-3.0, best_safe_vina=-3.0),
        _summary(1, best_vina=-3.2, best_safe_vina=None),   # no safe molecule
        _summary(2, best_vina=-3.6, best_safe_vina=-3.5),
    ]
    m = compute_agent_metrics(summaries, [], {})
    assert m["curves"]["best_safe_vina"] == [-3.0, None, -3.5]
    # first-to-last uses the first/last non-None values
    assert m["aggregates"]["best_safe_vina_first"] == -3.0
    assert m["aggregates"]["best_safe_vina_last"] == -3.5
    assert m["aggregates"]["best_safe_vina_delta"] == pytest.approx(-0.5)
    # verdict flags improvement on the safe objective
    assert m["verdict"]["run_safe_shows_improvement"] is True


def test_safe_vina_curve_absent_when_no_safe_rounds():
    """No safety-passing molecule in any round -> None curve, None delta."""
    summaries = [
        _summary(0, best_vina=-3.0, best_safe_vina=None),
        _summary(1, best_vina=-3.1, best_safe_vina=None),
    ]
    m = compute_agent_metrics(summaries, [], {})
    assert m["curves"]["best_safe_vina"] == [None, None]
    assert m["aggregates"]["best_safe_vina_first"] is None
    assert m["aggregates"]["best_safe_vina_last"] is None
    assert m["aggregates"]["best_safe_vina_delta"] is None
    assert m["verdict"]["run_safe_shows_improvement"] is None


def test_safe_vina_delta_worse_is_not_improvement():
    """A safe Vina that gets worse (positive delta) must not be flagged."""
    summaries = [
        _summary(0, best_vina=-3.5, best_safe_vina=-3.5),
        _summary(1, best_vina=-3.0, best_safe_vina=-3.0),
    ]
    m = compute_agent_metrics(summaries, [], {})
    assert m["aggregates"]["best_safe_vina_delta"] == pytest.approx(0.5)
    assert m["verdict"]["run_safe_shows_improvement"] is False
    assert "best_safe_vina" in m["verdict"]["run_safe_rationale"]


def test_safe_vina_rounds_without_safe_improvement_from_loop_state():
    """loop_state.rounds_without_safe_vina_improvement is surfaced."""
    summaries = [_summary(0, best_vina=-3.0, best_safe_vina=-3.0)]
    m = compute_agent_metrics(
        summaries, [], {"rounds_without_safe_vina_improvement": 2}
    )
    assert m["rounds_without_safe_improvement"] == 2
    # legacy key still present for backwards compatibility
    assert "rounds_without_improvement" in m


def test_safe_vina_output_json_safe():
    """The extended metrics block remains JSON-serialisable."""
    summaries = [_summary(0, best_vina=-3.0, best_safe_vina=-3.0)]
    m = compute_agent_metrics(summaries, [], {})
    json.dumps(m)
