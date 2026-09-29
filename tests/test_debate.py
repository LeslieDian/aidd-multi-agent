"""tests/test_debate.py - Unit tests for agents.debate."""
from __future__ import annotations

import pytest

from agents.debate import (
    DebateOutcome,
    DebateTurn,
    extract_evidence_ids,
    run_debate,
    should_terminate_debate,
    validate_critic_turn,
)


class TestExtractEvidenceIds:
    def test_hypothesis_id(self):
        eids = extract_evidence_ids("based on h:planned_s2 the metric is wrong")
        assert "h:planned_s2" in eids

    def test_proposal_id(self):
        eids = extract_evidence_ids("see p:r3:0 for the original proposal")
        assert "p:r3:0" in eids

    def test_multiple_ids(self):
        eids = extract_evidence_ids("h:s1 and h:s2 conflict; p:r1:2 supports s1")
        assert "h:s1" in eids
        assert "h:s2" in eids
        assert "p:r1:2" in eids

    def test_empty(self):
        assert extract_evidence_ids("") == ()
        assert extract_evidence_ids(None) == ()

    def test_no_evidence_id(self):
        assert extract_evidence_ids("looks fine to me") == ()


class TestValidateCriticTurn:
    def test_with_evidence(self):
        ok, reason = validate_critic_turn("rejected h:foo", require_evidence_id=True)
        assert ok is True
        assert "ok" in reason

    def test_without_evidence_rejected(self):
        ok, reason = validate_critic_turn("this is wrong", require_evidence_id=True)
        assert ok is False
        assert reason == "no_evidence_id"

    def test_not_required_passes(self):
        ok, _ = validate_critic_turn("no evidence here", require_evidence_id=False)
        assert ok is True


class TestShouldTerminate:
    def test_accept_keyword(self):
        turns = [
            DebateTurn("generator", "proposal text"),
            DebateTurn("critic", "ACCEPT"),
        ]
        terminate, reason = should_terminate_debate(turns, max_rounds=3)
        assert terminate is True
        assert reason == "critic_accepted"

    def test_max_rounds(self):
        turns = [
            DebateTurn("generator", f"turn {i} h:s{i}") for i in range(5)
        ]
        turns += [DebateTurn("critic", "still wrong h:s99")]
        terminate, reason = should_terminate_debate(turns, max_rounds=3)
        assert terminate is True
        assert reason == "max_rounds_reached"

    def test_continue_when_room(self):
        turns = [
            DebateTurn("generator", "g1"),
            DebateTurn("critic", "wrong h:s1"),
        ]
        terminate, reason = should_terminate_debate(turns, max_rounds=3)
        assert terminate is False
        assert reason == "continue"

    def test_empty_turns(self):
        terminate, reason = should_terminate_debate([], max_rounds=3)
        assert terminate is False
        assert reason == "no_turns"


class TestRunDebate:
    def test_accepted_path(self):
        outcome = run_debate(
            initial_generator_text="initial proposal h:s1",
            critic_turns=["wrong h:s1", "still wrong h:s1", "ACCEPT"],
            max_rounds=3,
        )
        assert outcome.accepted is True
        assert outcome.reason == "critic_accepted"
        assert outcome.rounds_used == 3

    def test_rejected_for_no_evidence(self):
        outcome = run_debate(
            initial_generator_text="initial proposal",
            critic_turns=["no evidence here"],
            max_rounds=3,
            require_evidence_id=True,
        )
        assert outcome.accepted is False
        assert "no_evidence_id" in outcome.reason

    def test_max_rounds(self):
        outcome = run_debate(
            initial_generator_text="initial h:s1",
            critic_turns=["wrong h:s1", "still wrong h:s1",
                          "again h:s1", "and again h:s1"],
            max_rounds=2,
        )
        assert outcome.accepted is False
        assert outcome.reason == "max_rounds_reached"

    def test_evidence_required_off(self):
        # With require_evidence_id=False, free-form challenges are allowed.
        outcome = run_debate(
            initial_generator_text="initial h:s1",
            critic_turns=["ACCEPT"],
            max_rounds=3,
            require_evidence_id=False,
        )
        assert outcome.accepted is True