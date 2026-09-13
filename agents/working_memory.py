"""agents/working_memory.py - Short-term context across rounds (Phase 4.1).

Holds:
- recent_rounds: last N round summaries (best Vina, valid count, scaffolds)
- strategy_chain: last N judge focus instructions
- best_so_far: best candidate seen in this session

Auto-truncates to `max_recent` so it never overflows the generator's context.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class RoundSummary:
    round: int
    n_valid: int
    n_total: int
    best_smiles: Optional[str]
    best_vina: Optional[float]
    best_composite: Optional[float]
    n_unique_scaffolds: int


class WorkingMemory:
    """In-session short-term memory for the AIDD loop."""

    def __init__(self, max_recent: int = 3):
        self.max_recent = max_recent
        self.recent_rounds: list[RoundSummary] = []
        self.strategy_chain: list[str] = []
        self.best_so_far: Optional[dict] = None  # full enriched candidate

    # ---------- writers ----------

    def add_round(
        self,
        candidates: list[dict],
        focus: str,
    ) -> None:
        """Called after evaluate_candidates() with the enriched list."""
        valid = [c for c in candidates if c.get("validate", {}).get("valid")]
        best = None
        for c in valid:
            score = c.get("dock", {}).get("score")
            if score is None:
                continue
            if best is None or score < best["dock"]["score"]:
                best = c

        summary = RoundSummary(
            round=len(self.recent_rounds),
            n_valid=len(valid),
            n_total=len(candidates),
            best_smiles=best["smiles"] if best else None,
            best_vina=best["dock"]["score"] if best else None,
            best_composite=best["composite_score"] if best else None,
            n_unique_scaffolds=len(
                {c.get("scaffold") for c in valid if c.get("scaffold")}
            ),
        )
        self.recent_rounds.append(summary)
        if len(self.recent_rounds) > self.max_recent:
            self.recent_rounds.pop(0)

        self.strategy_chain.append(focus)
        if len(self.strategy_chain) > self.max_recent:
            self.strategy_chain.pop(0)

        if best and (
            self.best_so_far is None
            or best["dock"]["score"] < self.best_so_far["dock"]["score"]
        ):
            self.best_so_far = best

    # ---------- readers ----------

    def compress_for_generator(self) -> str:
        """Generate a compact context block for the generator prompt."""
        if not self.recent_rounds:
            return "First round - no prior history."
        parts = [f"Memory of last {len(self.recent_rounds)} rounds:"]
        for r in self.recent_rounds:
            if r.best_vina is not None:
                parts.append(
                    f"  Round {r.round}: {r.n_valid}/{r.n_total} valid, "
                    f"best_Vina={r.best_vina:.2f}, "
                    f"{r.n_unique_scaffolds} scaffolds"
                )
            else:
                parts.append(
                    f"  Round {r.round}: {r.n_valid}/{r.n_total} valid"
                )
        if self.best_so_far:
            bv = self.best_so_far.get("dock", {}).get("score")
            if bv is not None:
                parts.append(
                    f"Best so far: Vina={bv:.2f}, "
                    f"smiles={self.best_so_far['smiles']}"
                )
        if self.strategy_chain:
            parts.append(
                "Previous strategies: " + " | ".join(
                    f"R{i}: {s[:60]}..." if len(s) > 60 else f"R{i}: {s}"
                    for i, s in enumerate(self.strategy_chain[-2:])
                )
            )
        return "\n".join(parts)

    def rounds_since_improvement(self) -> int:
        """Count consecutive rounds without new best Vina.

        Walks newest -> oldest. The first pair where a newer round beats the
        older one breaks the streak (improvement happened).
        """
        n = len(self.recent_rounds)
        if n < 2:
            return 0
        streak = 0
        for i in range(n - 1, 0, -1):
            newer = self.recent_rounds[i].best_vina
            older = self.recent_rounds[i - 1].best_vina
            if newer is None or older is None:
                streak += 1
                continue
            if newer < older:  # newer Vina is better (more negative)
                break
            streak += 1
        return streak

    def clear(self) -> None:
        self.recent_rounds.clear()
        self.strategy_chain.clear()
        self.best_so_far = None