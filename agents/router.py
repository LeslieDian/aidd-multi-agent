"""agents/router.py - Expert router (Phase 4.6 multi-agent).

The expert router activates different generator prompt templates based on
the current round state. It is *not* an LLM - it is a deterministic rule
engine that maps a state fingerprint to a prompt template name.

Activation rules (default):
    property_weak=True   -> prompt_qed_expert
    vina_weak=True       -> prompt_vina_expert
    sa_difficult=True    -> prompt_sa_expert
    (none of the above)  -> prompt_exploit_expert

Each expert prompt is just a string key. The actual prompt templates
live under agents/prompts/<expert>.j2 (Phase 4.6+ will add them; until
then the router still works as a switch and the loop falls back to the
default prompt).

The router is read from `loop.multi_agent.router` in config.yaml. If the
block is missing or `enabled: false`, the router is a no-op and the
existing single-template loop keeps working (backwards compatible).

Phase 4.7: when ``progress_signal == "safe_vina"`` (the project's preferred
signal), the router must also look at the safety-gated Vina. Otherwise the
router would still be watching all-candidate Vina even though the loop
optimises the safety-gated signal — a known inconsistency noted in the
REVIEW_MINIMAX_ADVICE_20260917.md audit (Priority B-1).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional


@dataclass(frozen=True)
class RoundFingerprint:
    """Snapshot of the current round's weak dimensions.

    Attributes:
        property_weak: True if property_score / QED / SA / hERG regressed
            (best-so-far delta in the last K rounds is negative or null).
        vina_weak: True if best (safety-gated) Vina regressed (best Vina
            has not improved in the last K rounds).
        sa_difficult: True if a recent SA-score alarm was raised
            (sa_score > 4 for top candidates).
    """

    property_weak: bool = False
    vina_weak: bool = False
    sa_difficult: bool = False

    @classmethod
    def from_history(
        cls,
        best_property_history: Iterable[float],
        best_vina_history: Iterable[float],
        recent_sa_scores: Iterable[float] = (),
        *,
        window: int = 2,
        sa_difficult_threshold: float = 4.0,
        best_safe_vina_history: Optional[Iterable[float]] = None,
        progress_signal: str = "vina",
    ) -> "RoundFingerprint":
        """Derive a fingerprint from the session's metric history.

        Args:
            best_property_history: best property_score per round (oldest -> newest).
            best_vina_history: best (all-candidates) safe_vina per round
                (oldest -> newest). Used when ``progress_signal="vina"``.
            recent_sa_scores: SA scores of the most recent top candidates.
            window: number of trailing rounds used to decide "weak".
            sa_difficult_threshold: SA score above which we flag SA as difficult.
            best_safe_vina_history: best safety-gated Vina per round. Used
                when ``progress_signal="safe_vina"`` (preferred signal).
            progress_signal: which Vina series the router should follow.
                Must be ``"vina"`` (legacy all-candidate) or
                ``"safe_vina"`` (preferred safety-gated).

        Returns:
            A RoundFingerprint suitable for the router.
        """
        prop_hist = list(best_property_history)[-window:]
        # property_weak: best has not improved over the window.
        property_weak = (
            len(prop_hist) >= 2 and prop_hist[-1] <= prop_hist[0]
        )
        # vina_weak: when progress_signal == "safe_vina" prefer the safety-gated
        # Vina history; otherwise fall back to the all-candidate Vina history.
        # Phase 4.7: this was previously always best_vina_history, so a project
        # that opted in to safe_vina got a router that still watched unsafe
        # progress.
        if progress_signal == "safe_vina" and best_safe_vina_history is not None:
            vina_series = [v for v in best_safe_vina_history if v is not None]
        else:
            vina_series = [v for v in best_vina_history if v is not None]
        vina_hist = vina_series[-window:]
        vina_weak = (
            len(vina_hist) >= 2 and vina_hist[-1] >= vina_hist[0]
        )
        sa_difficult = any(
            (s is not None and s > sa_difficult_threshold)
            for s in recent_sa_scores
        )
        return cls(
            property_weak=property_weak,
            vina_weak=vina_weak,
            sa_difficult=sa_difficult,
        )


def route(
    fp: RoundFingerprint,
    experts: dict[str, str] | None = None,
) -> str:
    """Pick the active expert prompt template based on the fingerprint.

    Args:
        fp: round fingerprint.
        experts: mapping from dimension key to expert prompt name. Defaults
            to the Phase 4.6 reference set.

    Returns:
        Expert prompt template name (string).

    Priority (highest first):
        1. sa_difficult (hard to fix -> dedicated SA expert)
        2. property_weak
        3. vina_weak
        4. otherwise -> the "ok" expert (exploitation)
    """
    if experts is None:
        experts = {
            "property_weak": "prompt_qed_expert",
            "vina_weak": "prompt_vina_expert",
            "sa_difficult": "prompt_sa_expert",
            "ok": "prompt_exploit_expert",
        }
    if fp.sa_difficult:
        return experts.get("sa_difficult", experts.get("ok", "default"))
    if fp.property_weak:
        return experts.get("property_weak", experts.get("ok", "default"))
    if fp.vina_weak:
        return experts.get("vina_weak", experts.get("ok", "default"))
    return experts.get("ok", "default")


def router_enabled(router_cfg: dict | None) -> bool:
    """Return True iff the router block is configured and enabled."""
    if not router_cfg:
        return False
    return bool(router_cfg.get("enabled", False))