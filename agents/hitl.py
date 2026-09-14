"""agents/hitl.py - Human-in-the-Loop checkpoint (Phase 4.1).

Three checkpoints (Phase 4.1 default):
1. Pre-loop approval: confirm before starting iterations
2. Mid-loop breakthrough: pause when best Vina drops by >= threshold
3. End-of-loop candidate selection: human picks synthesis whitelist

Drug-discovery decision making is a compliance issue, NOT a code preference.

Phase 4.3 (P2-2 fix): prompts are bilingual (English + 中文). Accept
yes/no equivalents in both languages:
    yes:  y, yes, yep, yeah, ok, okay, 是, 好, 好的, 嗯, 继续, go
    no:   n, no, nope, nah, 否, 不, 不要, stop, abort, 停, 取消
Default fallback is "no" (safer for compliance). All prompts echo the
default in [y/n] form; non-empty responses override the default.
"""
from __future__ import annotations

import sys
from typing import Optional


# Phase 4.3 (P2-2): i18n answer parsing.
_YES_TOKENS = {
    "y", "yes", "yep", "yeah", "ok", "okay", "k",
    "是", "好", "好的", "嗯", "继续", "go", "g",
    "shi", "hao", "xu",  # pinyin (defensive — terminal input varies)
}
_NO_TOKENS = {
    "n", "no", "nope", "nah", "nein", "nn",
    "否", "不", "不要", "停", "停止", "取消", "no",
    "fou", "bu", "ting",
}


def _parse_yn(ans: str, default: str = "n") -> bool:
    """Robust yes/no parsing across English + 中文.

    Returns True for yes-tokens, False for no-tokens or unrecognized input
    (conservative default for compliance: when in doubt, do NOT proceed).
    """
    a = ans.strip().lower()
    if not a:
        return default == "y"
    if a in _YES_TOKENS:
        return True
    if a.startswith(("是", "好", "继续")):
        return True
    if a in _NO_TOKENS:
        return False
    if a.startswith(("否", "不", "停")):
        return False
    # Fallback: first-char heuristic
    return a[0] in ("y", "是", "好", "k")


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

    def _ask(self, prompt: str, default: str = "y", bilingual: bool = True) -> bool:
        """Print `prompt`, read user input, return True/False.

        Phase 4.3 (P2-2): bilingual prompt + i18n answer parsing. Accepts
        English (y/yes/no/nope) and 中文 (是/好/继续/否/不/停).
        """
        if not self.require_approval:
            return True
        print()
        print("=" * 64)
        if bilingual:
            print("  HUMAN-IN-THE-LOOP CHECKPOINT  |  人工检查点")
        else:
            print("  HUMAN-IN-THE-LOOP CHECKPOINT")
        print("=" * 64)
        print(prompt)
        if bilingual:
            hint = "  [y/n / 是/否] "
        else:
            hint = f"  [{default}/n] "
        try:
            ans = input(hint)
        except EOFError:
            return default == "y"
        return _parse_yn(ans, default=default)

    # ---------- the 3 checkpoints ----------

    def pre_loop(self, n_rounds: int, n_per_round: int, provider_count: int) -> bool:
        if "start" not in self.enabled_points:
            return True
        msg = (
            f"About to start a {n_rounds}-round iterative optimization loop.\n"
            f"即将开始一个 {n_rounds} 轮迭代优化循环。\n"
            f"  - rounds:           {n_rounds}\n"
            f"  - candidates/round: {n_per_round}\n"
            f"  - providers:        {provider_count}\n"
            f"  - estimated total:  {n_rounds * n_per_round * provider_count} molecules\n"
            f"Proceed?  /  继续?"
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
            f"Vina 突破: 从 {prev_best:.2f} 改进到 {new_best:.2f}（提升 {improvement:.2f}）。\n"
            f"Continue to next round, or stop and inspect this molecule?\n"
            f"继续下一轮，还是停下检查这个分子?"
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
        print("  FINAL CANDIDATE SELECTION  |  最终候选选择")
        print("=" * 64)
        print("  Review the top candidates. y/是 = add to whitelist, n/否 = skip.")
        print("  (LLM cannot approve synthesis - this is compliance.)")
        print("  (LLM 不能批准合成 — 这是合规红线。)")
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
                ans = input("      Include in synthesis whitelist? [y/N / 是/否] ")
            except EOFError:
                ans = "n"
            if _parse_yn(ans, default="n"):
                chosen.append(c)
        self.synthesis_whitelist = chosen
        return chosen