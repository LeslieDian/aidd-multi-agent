"""agents/working_memory.py - Short-term context across rounds (Phase 4.1).

Holds:
- recent_rounds: last N round summaries (best Vina, valid count, scaffolds)
- strategy_chain: last N judge focus instructions
- best_so_far: best candidate seen in this session

Auto-truncates to `max_recent` so it never overflows the generator's context.

Phase 4.3 (P0-3 fix): strategy_chain is persisted across sessions to
`memory/strategy_history/<target>/<protocol_id>.json` so early-round wisdom
isn't lost when max_recent evicts it from the live list. The on-disk file
keeps the full history; the live `strategy_chain` is still capped at
`max_recent` for prompt-budget reasons.

Phase 4.3 (P1-1 fix): best_so_far is persisted to
`memory/best_molecules.json` (keyed by target) so a good molecule
discovered in a prior session is not forgotten on restart.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


DEFAULT_STRATEGY_DIR = Path("memory/strategy_history")
DEFAULT_BEST_MOLECULES_PATH = Path("memory/best_molecules.json")


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

    def __init__(
        self,
        max_recent: int = 3,
        strategy_persist_path: Optional[Path] = None,
        best_persist_path: Optional[Path] = None,
        target_name: Optional[str] = None,
    ):
        if max_recent < 1:
            raise ValueError("max_recent must be positive")
        self.total_rounds = 0
        self.max_recent = max_recent
        self.recent_rounds: list[RoundSummary] = []
        self.strategy_chain: list[str] = []
        self.best_so_far: Optional[dict] = None  # full enriched candidate

        # Phase 4.3 cross-session persistence
        self.strategy_persist_path = strategy_persist_path
        self.best_persist_path = best_persist_path
        self.target_name = target_name

        if strategy_persist_path:
            loaded = self._load_strategy_history()
            if loaded:
                # Keep full history on disk; live list seeded with last
                # max_recent so prompt injection sees continuity.
                self.strategy_chain = loaded[-max_recent:]
                self.total_rounds = len(loaded)
                # Don't restore recent_rounds / best_so_far here - those are
                # session-scoped by design (different runs may use different
                # protocols or seeds).
        if best_persist_path and target_name:
            self._load_best(target_name)

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
            round=self.total_rounds,
            n_valid=len(valid),
            n_total=len(candidates),
            best_smiles=best["smiles"] if best else None,
            best_vina=best["dock"]["score"] if best else None,
            best_composite=best["composite_score"] if best else None,
            n_unique_scaffolds=len(
                {c.get("scaffold") for c in valid if c.get("scaffold")}
            ),
        )
        self.total_rounds += 1
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

        # Phase 4.3 (P0-3): persist the FULL strategy history (not just the
        # truncated live list) so early-round wisdom survives across sessions.
        if self.strategy_persist_path:
            self._append_strategy_history(focus)

        # Phase 4.3 (P1-1): persist best-so-far whenever it improves
        if self.best_persist_path and self.target_name and self.best_so_far:
            self._save_best(self.target_name)

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
                    for i, s in enumerate(self.strategy_chain[-2:], start=self.total_rounds - min(2, len(self.strategy_chain)))
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
        self.total_rounds = 0
        self.recent_rounds.clear()
        self.strategy_chain.clear()
        self.best_so_far = None

    # ---------- Phase 4.3 cross-session persistence (P0-3, P1-1) ----------

    def _load_strategy_history(self) -> list[str]:
        """Load the FULL strategy history from disk (not capped)."""
        if not self.strategy_persist_path:
            return []
        try:
            if not self.strategy_persist_path.exists():
                return []
            data = json.loads(self.strategy_persist_path.read_text(encoding="utf-8"))
            chain = data.get("strategy_chain", [])
            return [str(s) for s in chain if s]
        except Exception:
            return []

    def _append_strategy_history(self, focus: str) -> None:
        """Append this round's focus to the on-disk history.

        Disk file keeps every focus ever recorded (no cap). The live
        `strategy_chain` is still capped at max_recent to protect prompt
        budget, but the next session's WorkingMemory will seed itself
        from the full disk history.
        """
        if not self.strategy_persist_path:
            return
        try:
            full = self._load_strategy_history()
            full.append(focus)
            self.strategy_persist_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "schema_version": 1,
                "target": self.target_name,
                "strategy_chain": full,
                "total_entries": len(full),
            }
            self.strategy_persist_path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            # Persistence is best-effort; never block the loop
            print(f"[WARN] failed to persist strategy history: {e}")

    def _load_best(self, target: str) -> None:
        """Load the persisted best-so-far for `target` if any.

        Only used to seed `self.best_so_far` at startup; a freshly
        discovered better molecule always overrides the persisted one.
        """
        if not self.best_persist_path:
            return
        try:
            if not self.best_persist_path.exists():
                return
            data = json.loads(self.best_persist_path.read_text(encoding="utf-8"))
            entry = data.get("targets", {}).get(target)
            if not entry:
                return
            score = entry.get("vina")
            smiles = entry.get("smiles")
            if score is None or smiles is None:
                return
            # Reconstruct a minimal candidate dict so callers can compare
            # via dock["score"] like the live best.
            self.best_so_far = {
                "smiles": smiles,
                "dock": {"score": score},
                "composite_score": entry.get("composite"),
                "validate": entry.get("validate", {"valid": True}),
                "admet": entry.get("admet", {}),
                "scaffold": entry.get("scaffold"),
                "provider": entry.get("provider"),
                "model": entry.get("model"),
                "round": entry.get("round"),
                "is_persisted_from_prior_session": True,
            }
        except Exception:
            return

    def _save_best(self, target: str) -> None:
        """Persist current best-so-far if it beats what's on disk."""
        if not self.best_persist_path or not self.best_so_far:
            return
        try:
            if self.best_persist_path.exists():
                data = json.loads(self.best_persist_path.read_text(encoding="utf-8"))
            else:
                data = {"schema_version": 1, "targets": {}}
            data.setdefault("schema_version", 1)
            data.setdefault("targets", {})
            existing = data["targets"].get(target)
            existing_score = existing.get("vina") if existing else None
            new_score = self.best_so_far.get("dock", {}).get("score")
            if new_score is None:
                return
            if existing_score is not None and new_score >= existing_score:
                return  # disk already has equal or better
            data["targets"][target] = {
                "smiles": self.best_so_far.get("smiles"),
                "vina": new_score,
                "composite": self.best_so_far.get("composite_score"),
                "scaffold": self.best_so_far.get("scaffold"),
                "provider": self.best_so_far.get("provider"),
                "model": self.best_so_far.get("model"),
                "round": self.best_so_far.get("round"),
                "validate": self.best_so_far.get("validate", {"valid": True}),
                "admet": self.best_so_far.get("admet", {}),
                "updated_at": self._now_iso(),
            }
            self.best_persist_path.parent.mkdir(parents=True, exist_ok=True)
            self.best_persist_path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            print(f"[WARN] failed to persist best molecule: {e}")

    @staticmethod
    def _now_iso() -> str:
        from datetime import datetime
        return datetime.now().isoformat(timespec="seconds")