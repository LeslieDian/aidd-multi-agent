"""agents/loop_controller.py - Explicit termination conditions for the AIDD loop.

Three hard stop conditions (Phase 4.1):
- max_rounds: hard cap on iterations
- token_budget: estimated token usage
- judge_convergence_patience: N rounds without Vina improvement

Plus a soft stop: HITL veto.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class LoopState:
    """Snapshot of loop state at the start of each round."""
    round: int = 0
    tokens_used: int = 0
    best_vina: Optional[float] = None
    rounds_without_vina_improvement: int = 0
    hitl_veto: bool = False
    last_round_best_vina: Optional[float] = None

    def note_round_result(self, round_best_vina: Optional[float]) -> None:
        """Call after each round to update convergence tracking."""
        self.last_round_best_vina = round_best_vina
        if round_best_vina is None:
            return
        if self.best_vina is None or round_best_vina < self.best_vina:
            self.best_vina = round_best_vina
            self.rounds_without_vina_improvement = 0
        else:
            self.rounds_without_vina_improvement += 1


@dataclass
class LoopConfig:
    max_rounds: int = 5
    token_budget: int = 50000
    judge_convergence_patience: int = 2


class LoopController:
    """Decides when the AIDD loop should terminate.

    Usage:
        ctrl = LoopController(LoopConfig(max_rounds=5, ...))
        state = LoopState()
        for r in range(N):
            ...
            state.note_round_result(best_vina_this_round)
            stop, reason = ctrl.should_stop(state)
            if stop:
                break
    """

    REASONS = {
        "max_rounds_reached":           "Reached max_rounds limit.",
        "token_budget_exceeded":        "Estimated token usage exceeded budget.",
        "no_improvement":              "No Vina improvement for N rounds (judge convergence).",
        "human_veto":                  "Human vetoed continuation.",
    }

    def __init__(self, config: LoopConfig | dict | None = None):
        if config is None:
            config = LoopConfig()
        if isinstance(config, dict):
            config = LoopConfig(**{k: v for k, v in config.items()
                                    if k in LoopConfig.__annotations__})
        self.config = config

    def should_stop(self, state: LoopState) -> tuple[bool, str]:
        """Return (stop?, reason_key)."""
        if state.hitl_veto:
            return True, "human_veto"
        if state.round >= self.config.max_rounds:
            return True, "max_rounds_reached"
        if state.tokens_used >= self.config.token_budget:
            return True, "token_budget_exceeded"
        if state.rounds_without_vina_improvement >= self.config.judge_convergence_patience:
            return True, "no_improvement"
        return False, ""

    def explain(self, reason: str) -> str:
        return self.REASONS.get(reason, reason)