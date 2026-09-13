"""agents/hitl.py - Human-in-the-Loop checkpoint (Phase 4.1).

Three checkpoints (Phase 4.1 default):
1. Pre-loop approval: confirm before starting iterations
2. Mid-loop breakthrough: pause when best Vina drops by >= threshold
3. End-of-loop candidate selection: human picks synthesis whitelist

Drug-discovery decision making is a compliance issue, NOT a code preference.
"""
from __future__ import annotations

import sys
from typing import Optional


class HITLCheckpoint:
    """Pauses the loop at predefined decision points."""

    def __init__(self, require_approval: bool = True, enabled_points: Optional[list[str]] = None):
        self.require_approval = require_approval
        self.enabled_points = set(
            enabled_points or ["start", "vina_breakthrough", "candidate_selection"]
        )
        self.veto = False
        self.synthesis_whitelist: list[dict] = []

    # ---------- generic helpers ----------

    def _ask(self, prompt: str, default: str = "y") -> bool:
        if not self.require_approval:
            return True
        print()
        print("=" * 64)
        print(f"  HUMAN-IN-THE-LOOP CHECKPOINT")
        print("=" * 64)
        print(prompt)
        try:
            ans = input(f"  [{default}/n] ").strip().lower()
        except EOFError:
            return default == "y"
        if not ans:
            return default == "y"
        return ans.startswith("y")

    # ---------- the 3 checkpoints ----------

    def pre_loop(self, n_rounds: int, n_per_round: int, provider_count: int) -> bool:
        if "start" not in self.enabled_points:
            return True
        msg = (
            f"About to start a {n_rounds}-round iterative optimization loop:\n"
            f"  - rounds: {n_rounds}\n"
            f"  - candidates per round per provider: {n_per_round}\n"
            f"  - providers: {provider_count}\n"
            f"  - estimated total: {n_rounds * n_per_round * provider_count} molecules\n"
            f"Proceed?"
        )
        return self._ask(msg)

    def on_vina_breakthrough(
        self,
        prev_best: Optional[float],
        new_best: float,
        improvement_threshold: float = 0.3,
    ) -> bool:
        """Called when best_vina drops by >= improvement_threshold vs prior best."""
        if "vina_breakthrough" not in self.enabled_points or prev_best is None:
            return True
        improvement = prev_best - new_best  # positive = better
        if improvement < improvement_threshold:
            return True
        msg = (
            f"Vina breakthrough: {prev_best:.2f} -> {new_best:.2f} "
            f"(improved by {improvement:.2f}).\n"
            f"Continue to next round, or stop and inspect this molecule?"
        )
        ans = self._ask(msg)
        if not ans:
            self.veto = True
        return ans

    def select_synthesis_candidates(self, top_candidates: list[dict]) -> list[dict]:
        """End-of-loop: human picks which molecules go into synthesis whitelist."""
        if "candidate_selection" not in self.enabled_points or not top_candidates:
            return top_candidates[:3]
        print()
        print("=" * 64)
        print("  FINAL CANDIDATE SELECTION")
        print("=" * 64)
        print("  Review the top candidates. y = add to synthesis whitelist, n = skip.")
        print("  (Decision: LLM cannot approve synthesis - this is compliance.)")
        chosen: list[dict] = []
        for i, c in enumerate(top_candidates[:5]):
            v = c.get("validate", {})
            a = c.get("admet", {})
            d = c.get("dock", {})
            print(f"\n  [{i}] {c['smiles']}")
            print(f"      MW={v.get('mw', '?')} logP={v.get('logp', '?')} "
                  f"SA={v.get('sa_score', '?')}")
            print(f"      ADMET={a.get('summary_score', '?'):.3f} "
                  f"Vina={d.get('score', '?'):.2f}")
            try:
                ans = input("      Include in synthesis whitelist? [y/N] ").strip().lower()
            except EOFError:
                ans = "n"
            if ans.startswith("y"):
                chosen.append(c)
        self.synthesis_whitelist = chosen
        return chosen