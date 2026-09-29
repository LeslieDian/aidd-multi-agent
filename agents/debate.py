"""agents/debate.py - Adversarial generator <-> critic debate (Phase 4.6).

The adversarial debate is triggered when judges disagree (max - median
>= disagreement_threshold) or any judge score < confidence_floor.

In each debate round:
  1. Generator proposes a candidate set with explicit reasoning.
  2. Critic pushes back, requiring an evidence ID (h:<hypothesis_id> or
     p:<proposal_id>:<option_index>) for every challenge.
  3. Generator may revise; loop ends when critic accepts OR max_rounds is hit.

This module is *deterministic glue*: it tracks debate state, decides when
to stop, and exposes a single `run_debate_round` that delegates the LLM
calls to the existing `judge_round` / `generator` modules. Critically:

  - require_evidence_id=True is the *default*; without an evidence ID,
    the critic's challenge is dropped from the accepted-verdict log.
  - Debate stops early on accept / convergence (no challenge in last round).

NOT shipped yet (Phase 4.6 stage 5 follow-ups):
  - LLM call implementations (we wire the loop; the actual LLM prompts
    are templated and read from agents.prompts).
  - Persistence into the local Repository / per-round JSONL.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

# An evidence ID is either h:<hypothesis_id> or p:<proposal_id>:<option_index>.
_EVIDENCE_RE = re.compile(r"(?:^|\s)([hp]:[A-Za-z0-9_\-:.]+)(?:\s|$|[,.;])")


@dataclass
class DebateTurn:
    """One turn of an adversarial debate (generator OR critic)."""

    role: str           # "generator" or "critic"
    text: str
    evidence_ids: tuple[str, ...] = field(default_factory=tuple)


@dataclass
class DebateOutcome:
    """Result of an adversarial debate round."""

    turns: list[DebateTurn]
    accepted: bool
    reason: str
    rounds_used: int


def extract_evidence_ids(text: str) -> tuple[str, ...]:
    """Extract evidence IDs from a critic / generator text.

    Returns the unique evidence IDs found. An empty input returns ().
    """
    if not text:
        return ()
    found = []
    seen = set()
    for m in _EVIDENCE_RE.finditer(text):
        eid = m.group(1)
        if eid in seen:
            continue
        seen.add(eid)
        found.append(eid)
    return tuple(found)


def validate_critic_turn(
    text: str,
    *,
    require_evidence_id: bool,
) -> tuple[bool, str]:
    """Validate a critic turn. Return (ok, reason).

    Rules:
    - If `require_evidence_id=False`, every turn is OK.
    - If the turn contains the ACCEPT keyword (`ACCEPT`, `accept(ed)?`,
      or `ok(...)`), it terminates the debate and is OK without
      requiring an evidence ID (acceptance is itself the verdict).
    - Otherwise, the critic MUST cite at least one evidence ID per turn.
    """
    if not require_evidence_id:
        return True, "evidence_id_not_required"
    # ACCEPT-style turns bypass the evidence-id requirement (they are the
    # verdict, not a challenge).
    if re.search(r"\bACCEPT\b|\baccept(ed)?\b|\bok\s*\(", text or "", flags=re.IGNORECASE):
        return True, "accept_terminates"
    eids = extract_evidence_ids(text)
    if not eids:
        return False, "no_evidence_id"
    return True, f"ok({len(eids)}_ids)"


def should_terminate_debate(
    turns: list[DebateTurn],
    *,
    max_rounds: int,
    convergence_window: int = 1,
) -> tuple[bool, str]:
    """Decide whether the debate has converged or maxed out.

    Termination conditions (checked in this order):
      1. Any critic turn contains "ACCEPT" / "accept" / "ok" -> accepted.
      2. Last `convergence_window` critic turns have no challenges -> no_progress.
      3. Generator revisions used >= max_rounds -> max_rounds_reached.

    `max_rounds` is the maximum number of *revisions* allowed (initial
    proposal does NOT count as a revision).
    """
    if not turns:
        return False, "no_turns"
    # revisions = generator turns minus the initial proposal (always the first
    # generator turn, if any).
    gen_turns = [t for t in turns if t.role == "generator"]
    revisions = max(0, len(gen_turns) - 1)
    critic_turns = [t for t in turns if t.role == "critic"]
    # 1. Acceptance keyword (highest priority).
    for t in critic_turns:
        if re.search(r"\bACCEPT\b|\baccept(ed)?\b|\bok\s*\(", t.text, flags=re.IGNORECASE):
            return True, "critic_accepted"
    # 2. Convergence window.
    if len(critic_turns) >= convergence_window:
        tail = critic_turns[-convergence_window:]
        all_blank = all(not t.text.strip() or "OK" in t.text.upper() for t in tail)
        if all_blank:
            return True, "converged"
    # 3. Max revisions.
    if revisions >= max_rounds:
        return True, "max_rounds_reached"
    return False, "continue"


def run_debate(
    *,
    initial_generator_text: str,
    critic_turns: Iterable[str],
    max_rounds: int,
    require_evidence_id: bool = True,
) -> DebateOutcome:
    """Run a synthetic debate. Used by tests / offline smoke.

    Each entry in `critic_turns` is one critic response; the generator
    "responds" with a synthetic mirror turn. Real wiring would have a
    generator LLM call instead.

    Termination checks happen after EACH critic turn. An ACCEPT keyword
    terminates immediately (no generator mirror turn follows acceptance).
    `max_rounds` is the maximum number of *generator* revisions allowed.
    """
    turns: list[DebateTurn] = []
    turns.append(DebateTurn(
        role="generator",
        text=initial_generator_text,
        evidence_ids=extract_evidence_ids(initial_generator_text),
    ))
    critic_list = list(critic_turns)
    revisions_used = 0
    for i, ct in enumerate(critic_list):
        eids = extract_evidence_ids(ct)
        ok, reason = validate_critic_turn(ct, require_evidence_id=require_evidence_id)
        if not ok:
            turns.append(DebateTurn(
                role="critic",
                text=ct,
                evidence_ids=(),
            ))
            return DebateOutcome(
                turns=turns,
                accepted=False,
                reason=f"critic_rejected:{reason}",
                rounds_used=revisions_used,
            )
        turns.append(DebateTurn(
            role="critic",
            text=ct,
            evidence_ids=eids,
        ))
        # Check immediate termination (ACCEPT keyword).
        terminate, term_reason = should_terminate_debate(turns, max_rounds=max_rounds)
        if terminate:
            # If accepted, terminate without a generator mirror turn.
            # rounds_used for an ACCEPT after i critic turns is i + 1
            # (the initial proposal + i generator revisions).
            return DebateOutcome(
                turns=turns,
                accepted=(term_reason == "critic_accepted"),
                reason=term_reason,
                rounds_used=revisions_used + 1,
            )
        # Synthetic generator mirror turn (one revision per critic turn).
        revisions_used += 1
        turns.append(DebateTurn(
            role="generator",
            text=f"<gen-rev {revisions_used}> responding to critic with evidence {','.join(eids)}",
            evidence_ids=eids,
        ))
        # Re-check after generator revision (max_rounds / convergence).
        terminate, term_reason = should_terminate_debate(turns, max_rounds=max_rounds)
        if terminate:
            return DebateOutcome(
                turns=turns,
                accepted=(term_reason == "critic_accepted"),
                reason=term_reason,
                rounds_used=revisions_used,
            )
    return DebateOutcome(
        turns=turns,
        accepted=False,
        reason="exhausted_critic_turns",
        rounds_used=revisions_used,
    )