"""Regression tests for the benzonitrile schema errors observed on 2026-09-21.

The benzonitrile P1 run hit 5 schema errors and 1 state_machine error in 17 steps.
These tests reproduce each schema error from the recorded events and assert that
the validator can now recover without rejecting the action.

Observed failure modes (from runs/diagnostic_2d_p1_20260921_benzonitrile/agent/task.json):

Event 14: choose_strategy with "rationale" inside arguments instead of top-level reason.
  {"tool":"choose_strategy","arguments":{"hypothesis_id":"planned_s1","choice":"switch_strategy",
   "parent_id":"c1","rationale":"..."}}
  Error: missing=['reason']; allowed=['arguments', 'reason', 'tool']

Event 22: attach_fragment with operation/arguments envelope instead of tool/arguments.
  {"operation":"attach_fragment","arguments":{"atom_index":1,"fragment_smiles":"O",
   "fragment_atom_index":0},"rationale":"...","expected_benefit":"..."}
  Error: missing=['reason','tool']; unexpected=[many]

Event 30: choose_strategy with extra bookkeeping fields in arguments.
  {"tool":"choose_strategy","arguments":{"hypothesis_id":"planned_s2","choice":"switch_strategy",
   "parent_id":"c1","rationale":"...","revision":0,"step":15,"basis":"inconclusive",
   "status":"executed","next_hypothesis_id":"planned_s3"},"reason":"..."}
  Error: unexpected=['basis','next_hypothesis_id','revision','status','step'];
  allowed=['choice','hypothesis_id','parent_id','rationale']

Event 34: propose_edits with rich per-option metadata (expected_benefit, predictions...).
  Each option has extra fields beyond the spec; arguments doesn't match schema.

Event 39: choose_strategy with rationale instead of reason again (same as Event 14).

These tests exercise the chosen sanitization strategy:
1. Accept "rationale" as a top-level fallback for "reason".
2. Drop unknown top-level keys (model drift / verbose output).
3. For tool arguments: drop unknown keys with a structured error so the next attempt
   can correct; this is for the inner schema, not the envelope.
"""
from __future__ import annotations

import pytest

from agents.harness.tools import ToolRegistry, default_registry


def _registry():
    return default_registry()


def test_choose_strategy_with_rationale_in_arguments_uses_rationale_as_reason():
    """Event 14 / Event 39 reproduction."""
    reg = _registry()
    action = {
        "tool": "choose_strategy",
        "arguments": {
            "hypothesis_id": "planned_s1",
            "choice": "switch_strategy",
            "parent_id": "c1",
            "rationale": "c2 仍未达 min_effect; 改换 parent",
        },
    }
    tool = reg.validate(action)
    assert tool.name == "choose_strategy"
    # rationale must have been promoted to top-level reason
    assert action["reason"].startswith("c2")


def test_attach_fragment_with_operation_envelope_is_recovered():
    """Event 22 reproduction: model wrote operation+arguments instead of tool+arguments.

    The actual benzonitrile model output was missing hypothesis_id/parent_id (required
    arguments for attach_fragment), so the action genuinely could not be recovered.
    Here we verify the recovery succeeds when ALL required arguments are present
    but the envelope shape is wrong (operation vs tool + extra top-level metadata).
    """
    reg = _registry()
    action = {
        "operation": "attach_fragment",
        "arguments": {
            "atom_index": 1,
            "fragment_smiles": "O",
            "fragment_atom_index": 0,
            "hypothesis_id": "planned_s1",
            "parent_id": "c1",
        },
        "rationale": "在氰基N远端引入羟基",
        "expected_benefit": "property_score 微调",
        "allowed_cost": "可能无效",
    }
    tool = reg.validate(action)
    assert tool.name == "attach_fragment"
    assert action["tool"] == "attach_fragment"
    # extra top-level keys should have been dropped
    assert "operation" not in action
    assert "expected_benefit" not in action
    assert "allowed_cost" not in action
    # reason recovered from rationale
    assert "氰基N远端" in action["reason"]


def test_choose_strategy_with_extra_arguments_keys_strips_them():
    """Event 30 reproduction: extra bookkeeping fields inside arguments."""
    reg = _registry()
    action = {
        "tool": "choose_strategy",
        "arguments": {
            "hypothesis_id": "planned_s2",
            "choice": "switch_strategy",
            "parent_id": "c1",
            "rationale": "切策略以提议新一批目录候选",
            "revision": 0,
            "step": 15,
            "basis": "inconclusive",
            "status": "executed",
            "next_hypothesis_id": "planned_s3",
        },
        "reason": "切策略",
    }
    tool = reg.validate(action)
    assert tool.name == "choose_strategy"
    # Only the schema-specified keys remain in arguments
    allowed_keys = {"hypothesis_id", "choice", "parent_id", "rationale"}
    assert set(action["arguments"]) == allowed_keys


def test_top_level_extra_keys_are_stripped():
    """Top-level envelope must be strict, but unknown extras are dropped, not rejected."""
    reg = _registry()
    action = {
        "tool": "pause",
        "arguments": {"message": "Need user input"},
        "reason": "waiting",
        "step": 7,
        "basis": "rationale",
        "next_hypothesis_id": "planned_s1",
    }
    tool = reg.validate(action)
    assert tool.name == "pause"
    # extras dropped
    assert "step" not in action
    assert "basis" not in action
    assert "next_hypothesis_id" not in action


def test_no_reason_no_rationale_still_rejected():
    """If neither reason nor rationale is present, the validator still rejects."""
    reg = _registry()
    action = {
        "tool": "pause",
        "arguments": {"message": "Need user input"},
    }
    with pytest.raises(ValueError, match="reason"):
        reg.validate(action)


def test_envelope_must_still_have_tool_and_arguments():
    """Sanitization does not invent missing tool or arguments keys."""
    reg = _registry()
    # missing tool
    with pytest.raises(ValueError):
        reg.validate({"arguments": {}, "reason": "x"})
    # missing arguments
    with pytest.raises(ValueError):
        reg.validate({"tool": "pause", "reason": "x"})


def test_unknown_tool_still_rejected():
    """Sanitization does not rescue an unknown tool name."""
    reg = _registry()
    action = {
        "tool": "invent_a_new_tool",
        "arguments": {},
        "reason": "trial",
    }
    with pytest.raises(ValueError, match="Unknown tool"):
        reg.validate(action)


def test_empty_registry_rejects_any_action():
    """An empty registry rejects everything except the structural envelope."""
    reg = ToolRegistry()
    with pytest.raises(ValueError, match="Unknown tool"):
        reg.validate({"tool": "evaluate", "arguments": {"candidate_ids": ["c1"]}, "reason": "test"})