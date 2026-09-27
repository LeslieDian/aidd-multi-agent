"""Integration tests for the runtime wiring of prediction_error.

The unit test (test_prediction_error.py) covers ``RuleStore.add_prediction_error``
in isolation. These tests cover the end-to-end path through
``LLMPolicy.update_memory_from_events``:

* an ``evaluate_options`` tool_result with outcome="insufficient_evidence"
  must produce an EVIDENCE rule keyed by (parent, child, direction)
* the rule's predicted_delta / observed_delta must match the option
  prediction and the screening row's observed_delta
* duplicate observations must accumulate on the same rule
* a rule_store storage failure must NOT be silent: a ``rule_store_error``
  audit event must reach the evidence sink (or at minimum stderr) and the
  loop must continue

These tests construct a synthetic state object — the goal is to lock down
the runtime contract without booting the full LLM harness.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agents.rule_memory import EVIDENCE, RuleStore
from agents.harness.runtime import LLMPolicy


# --- minimal state double ---------------------------------------------------


def _make_state(c1_smiles: str = "Oc1ccccc1", scaffold: str = "phenol"):
    """A state double with just enough surface for update_memory_from_events."""
    c1 = {"smiles": c1_smiles, "scaffold": scaffold, "candidate_id": "c1"}
    return SimpleNamespace(
        candidates={"c1": c1},
        total_rounds=1,
        constraints={},
        hypotheses={},
        edit_selections={},
        edit_proposals={},
        config={},
    )


def _eval_event(*, predicted_min_change, observed_delta, child_smiles="COc1ccccc1",
                 parent_id="c1", parent_smiles="Oc1ccccc1", opt_index=0):
    """Build a synthetic tool_result event for evaluate_options.

    Mirrors the shape LLMPolicy.update_memory_from_events reads:
    * action.arguments.parent_id
    * action.arguments.options[].option_index, .precheck.product_smiles,
      .predictions[].metric, .min_change
    * result.screening_comparison.rows[].option_index, .effect.outcome,
      .effect.observed_delta
    """
    return {
        "type": "tool_result",
        "action": {
            "tool": "evaluate_options",
            "arguments": {
                "parent_id": parent_id,
                "options": [
                    {
                        "option_index": opt_index,
                        "precheck": {"product_smiles": child_smiles},
                        "predictions": [
                            {"metric": "property_score",
                             "min_change": predicted_min_change},
                        ],
                    }
                ],
            },
        },
        "result": {
            "screening_comparison": {
                "rows": [
                    {
                        "option_index": opt_index,
                        "effect": {
                            "outcome": "insufficient_evidence",
                            "observed_delta": observed_delta,
                        },
                    }
                ]
            }
        },
        "_parent_smiles": parent_smiles,
    }


# --- tests ------------------------------------------------------------------


def test_insufficient_evidence_emits_evidence_rule():
    """One insufficient_evidence row -> exactly one EVIDENCE rule."""
    store = RuleStore()
    policy = LLMPolicy(rule_store=store, task_id="EGFR")
    state = _make_state()
    events = [_eval_event(predicted_min_change=0.02, observed_delta=0.005)]
    policy.update_memory_from_events(events, state)
    evidence_rules = store.by_category(EVIDENCE)
    assert len(evidence_rules) == 1
    rule = evidence_rules[0]
    assert rule.pattern["parent_smiles"] == "Oc1ccccc1"
    assert rule.pattern["child_smiles"] == "COc1ccccc1"
    assert rule.pattern["direction"] == "under"
    assert "0.0200" in rule.description and "0.0050" in rule.description
    policy.close()


def test_over_direction_when_observed_exceeds_predicted():
    """predicted < observed -> direction='over'."""
    store = RuleStore()
    policy = LLMPolicy(rule_store=store, task_id="EGFR")
    state = _make_state()
    events = [_eval_event(predicted_min_change=0.005, observed_delta=0.020)]
    policy.update_memory_from_events(events, state)
    [rule] = store.by_category(EVIDENCE)
    assert rule.pattern["direction"] == "over"
    policy.close()


def test_duplicate_parent_child_pair_accumulates():
    store = RuleStore()
    policy = LLMPolicy(rule_store=store, task_id="EGFR")
    state = _make_state()
    for _ in range(3):
        policy.update_memory_from_events(
            [_eval_event(predicted_min_change=0.02, observed_delta=0.005)],
            state,
        )
    [rule] = store.by_category(EVIDENCE)
    assert rule.observations == 3
    policy.close()


def test_supported_and_tradeoff_outcomes_do_not_pollute_evidence():
    """sanity: only insufficient_evidence feeds the EVIDENCE category."""
    store = RuleStore()
    policy = LLMPolicy(rule_store=store, task_id="EGFR")
    state = _make_state()
    # supported
    supported = _eval_event(predicted_min_change=0.02, observed_delta=0.025)
    supported["result"]["screening_comparison"]["rows"][0]["effect"]["outcome"] = "supported"
    # tradeoff_exceeded
    tradeoff = _eval_event(predicted_min_change=0.02, observed_delta=-0.10, opt_index=1)
    tradeoff["result"]["screening_comparison"]["rows"][0]["effect"]["outcome"] = "tradeoff_exceeded"
    policy.update_memory_from_events([supported, tradeoff], state)
    assert store.by_category(EVIDENCE) == []
    # supported should produce a positive_transformation
    assert len(store.by_category("positive_transformation")) == 1
    # tradeoff should produce a negative_constraint
    assert len(store.by_category("negative_constraint")) == 1
    policy.close()


def test_missing_predicted_delta_skips_silently():
    """An option without a property_score prediction cannot record error."""
    store = RuleStore()
    policy = LLMPolicy(rule_store=store, task_id="EGFR")
    state = _make_state()
    event = _eval_event(predicted_min_change=0.02, observed_delta=0.005)
    # Replace predictions with a metric that update_memory_from_events
    # doesn't recognise (so opts_predictions stays empty).
    event["action"]["arguments"]["options"][0]["predictions"] = [
        {"metric": "logP", "min_change": 0.5}
    ]
    policy.update_memory_from_events([event], state)
    assert store.by_category(EVIDENCE) == []
    policy.close()


def test_rule_store_error_is_audited_not_swallowed():
    """When RuleStore.add_prediction_error raises, the loop must emit a
    ``rule_store_error`` audit event to the evidence sink AND a stderr line,
    then continue processing other events."""
    store = RuleStore()
    policy = LLMPolicy(rule_store=store, task_id="EGFR")

    # Force the storage path to raise. We patch the method itself.
    original = store.add_prediction_error
    call_count = {"n": 0}

    def boom(**kwargs):
        call_count["n"] += 1
        raise RuntimeError("disk full")

    store.add_prediction_error = boom

    captured = []

    def sink(event):
        captured.append(event)

    policy.evidence = sink

    state = _make_state()
    policy.update_memory_from_events(
        [_eval_event(predicted_min_change=0.02, observed_delta=0.005)],
        state,
    )

    # Storage was attempted
    assert call_count["n"] == 1
    # An audit event was emitted with a sanitized exception, NOT silent
    assert captured, "evidence sink never received the rule_store_error event"
    event = captured[-1]
    assert event["type"] == "rule_store_error"
    assert event["rule_category"] == "evidence_strength"
    assert event["operation"] == "add_prediction_error"
    assert "disk full" in event["exception"]

    # Restore and verify a subsequent valid event still gets recorded
    store.add_prediction_error = original
    policy.update_memory_from_events(
        [_eval_event(predicted_min_change=0.02, observed_delta=0.005,
                     child_smiles="CCOc1ccccc1")],
        state,
    )
    assert len(store.by_category(EVIDENCE)) == 1
    policy.close()


def test_non_evaluate_options_events_are_ignored():
    """Only evaluate_options feeds calibration. Other tools must be no-ops
    for the EVIDENCE category."""
    store = RuleStore()
    policy = LLMPolicy(rule_store=store, task_id="EGFR")
    state = _make_state()
    policy.update_memory_from_events(
        [{"type": "tool_result", "action": {"tool": "history"},
          "result": {}}],
        state,
    )
    assert store.by_category(EVIDENCE) == []
    policy.close()