"""agents/loop_controller.py - Explicit termination conditions for the AIDD loop.

Three hard stop conditions (Phase 4.1):
- max_rounds: hard cap on iterations
- token_budget: estimated token usage
- judge_convergence_patience: N rounds without improvement of the
  configured progress signal

Plus a soft stop: HITL veto.

Progress signal (2026-09-17):
`LoopConfig.progress_signal` selects which per-round number the patience
counter watches:

- ``"vina"``      legacy: the best docking score over EVERY docked candidate.
  This signal ignores the safety gate, so a molecule with excellent Vina and a
  bad hERG/logP profile counts as progress and resets the patience counter.
  Measured on `benchmarks/confirmatory_pareto_v3_1_20260916`: the all-candidate
  optimum was a herg_risk=0.830 molecule, while a safety-passing molecule sat
  only 0.020 kcal/mol behind. The regression on hERG therefore came from what
  the loop treated as progress, not from an unavoidable docking/safety
  trade-off.
- ``"safe_vina"`` preferred: the best docking score among safety-gate-passing
  candidates only. A round where nothing passes the gate is not progress.

The class default stays ``"vina"`` so existing callers and tests keep legacy
behaviour; real experiments opt in via ``loop.progress_signal`` in config.yaml,
and the choice is stamped into `manifest.json` so runs remain auditable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


PROGRESS_SIGNALS = ("vina", "safe_vina")


@dataclass
class LoopState:
    """Snapshot of loop state at the start of each round.

    `best_vina` / `rounds_without_vina_improvement` track the legacy
    all-candidates signal. `best_safe_vina` /
    `rounds_without_safe_vina_improvement` track the safety-gated signal.
    Both are always maintained so that a round record carries enough
    evidence to audit either termination policy after the fact.
    """
    round: int = 0
    tokens_used: int = 0
    best_vina: Optional[float] = None
    rounds_without_vina_improvement: int = 0
    best_safe_vina: Optional[float] = None
    rounds_without_safe_vina_improvement: int = 0
    hitl_veto: bool = False
    last_round_best_vina: Optional[float] = None
    last_round_best_safe_vina: Optional[float] = None

    def note_round_result(self, round_best_vina: Optional[float]) -> None:
        """Legacy signal: best Vina over all docked candidates."""
        self.last_round_best_vina = round_best_vina
        if round_best_vina is None:
            return
        if self.best_vina is None or round_best_vina < self.best_vina:
            self.best_vina = round_best_vina
            self.rounds_without_vina_improvement = 0
        else:
            self.rounds_without_vina_improvement += 1

    def note_round_safe_result(self, round_best_safe_vina: Optional[float]) -> None:
        """Safety-gated signal: best Vina among safety-gate-passing candidates.

        A round with no safety-passing candidate yields ``None``. Following the
        project rule that "missing scores are unknown, not success", ``None``
        neither counts as improvement nor as evidence of a plateau, so the
        counter is left untouched. This also keeps ``--no-dock`` smoke runs
        (where every score is ``None``) from terminating early.
        """
        self.last_round_best_safe_vina = round_best_safe_vina
        if round_best_safe_vina is None:
            return
        if self.best_safe_vina is None or round_best_safe_vina < self.best_safe_vina:
            self.best_safe_vina = round_best_safe_vina
            self.rounds_without_safe_vina_improvement = 0
        else:
            self.rounds_without_safe_vina_improvement += 1


@dataclass
class LoopConfig:
    max_rounds: int = 5
    token_budget: int = 50000
    judge_convergence_patience: int = 2
    # "vina" (legacy, all candidates) or "safe_vina" (safety-gated).
    progress_signal: str = "vina"


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
        "no_improvement":              "No improvement of the progress signal for N rounds (judge convergence).",
        "human_veto":                  "Human vetoed continuation.",
    }

    def __init__(self, config: LoopConfig | dict | None = None):
        if config is None:
            config = LoopConfig()
        if isinstance(config, dict):
            config = LoopConfig(**{k: v for k, v in config.items()
                                    if k in LoopConfig.__annotations__})
        if config.progress_signal not in PROGRESS_SIGNALS:
            raise ValueError(
                f"loop.progress_signal must be one of {PROGRESS_SIGNALS}, "
                f"got {config.progress_signal!r}"
            )
        self.config = config

    @property
    def progress_signal(self) -> str:
        return self.config.progress_signal

    def rounds_without_improvement(self, state: LoopState) -> int:
        """Patience counter for the configured signal."""
        if self.config.progress_signal == "safe_vina":
            return state.rounds_without_safe_vina_improvement
        return state.rounds_without_vina_improvement

    def should_stop(self, state: LoopState) -> tuple[bool, str]:
        """Return (stop?, reason_key)."""
        if state.hitl_veto:
            return True, "human_veto"
        if state.round >= self.config.max_rounds:
            return True, "max_rounds_reached"
        if state.tokens_used >= self.config.token_budget:
            return True, "token_budget_exceeded"
        if self.rounds_without_improvement(state) >= self.config.judge_convergence_patience:
            return True, "no_improvement"
        return False, ""

    def explain(self, reason: str) -> str:
        if reason == "no_improvement":
            return (f"No improvement of '{self.config.progress_signal}' for "
                    f"{self.config.judge_convergence_patience} rounds "
                    f"(judge convergence).")
        return self.REASONS.get(reason, reason)