"""4-category rule memory (extends the simple failed-set blacklist).

The CONFIRMATORY_RESULTS_20260916 do_not_approve verdict flagged that
"failed_set alone is a suppressor, not an optimizer". The recommendation
was to split memory into four orthogonal categories:

  1. NEGATIVE constraints
     (existing FailedLigandSet): "do NOT propose these SMILES".
     Source: rejected candidates with measured low scores.

  2. POSITIVE structural transformations
     "attach fragment X at site Y improved property_score by Z".
     Source: committed candidates (goal_met) and their parent-child diff.

  3. APPLICABLE context
     "this rule applies to scaffolds of class C1, C2 (NOT C3)".
     Source: scaffold fingerprints of successful vs failed applications.

  4. EVIDENCE strength
     "rule R has been observed N times with mean success S and CI width W".
     Source: rolling statistics over rule application history.

The four categories must NOT collapse back into a single "good vs bad"
flag, because the confirmatory verdict showed that monolithic memory
leaves no recoverable signal when the search space is large.

Retrieval contract:
    retrieve(context: dict) -> list[Rule]
    `context` carries the current parent_smiles, scaffold, available actions,
    and constraints. The returned rules are sorted by evidence_strength * context_relevance.
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional


# ---------- Category enum ----------

NEGATIVE = "negative_constraint"
POSITIVE = "positive_transformation"
CONTEXT = "applicable_context"
EVIDENCE = "evidence_strength"

CATEGORIES = (NEGATIVE, POSITIVE, CONTEXT, EVIDENCE)


@dataclass
class Rule:
    """A single memory record.

    Categories are mutually-exclusive, but a complex learning event may emit
    several Rules (one NEGATIVE for the failed child, one POSITIVE for the
    successful parent-child edit, one CONTEXT for the scaffold fingerprint,
    one EVIDENCE for the support count update).
    """
    rule_id: str
    category: str
    description: str
    pattern: dict = field(default_factory=dict)
    applicable_context: list[str] = field(default_factory=list)
    evidence_strength: float = 0.0
    observations: int = 0
    successes: int = 0
    failures: int = 0
    created_utc: str = ""
    last_updated_utc: str = ""
    source_round: Optional[int] = None
    source_smiles: Optional[str] = None
    parent_smiles: Optional[str] = None

    def update_evidence(self, *, success: bool, weight: float = 1.0) -> None:
        """Bayesian-ish update: log-odds shift by `weight` per observation."""
        self.observations += 1
        if success:
            self.successes += 1
        else:
            self.failures += 1
        # Map (successes, failures) to [0, 1] via Laplace-smoothed Wilson center.
        n = self.observations
        s = self.successes
        # Wilson lower bound at 95% confidence (smoothing by +2).
        z = 1.96
        denom = n + z * z
        centre = (s + z * z / 2) / denom
        margin = z * math.sqrt((s * (n - s) + z * z / 4) / n) / denom
        # Floor at 0 for low-sample rule (avoid over-confident early signal)
        if n < 3:
            self.evidence_strength = s / max(1, n)
        else:
            self.evidence_strength = max(0.0, centre - margin)
        self.last_updated_utc = datetime.now(timezone.utc).isoformat()


class RuleStore:
    """In-memory + on-disk 4-category rule memory."""

    def __init__(self, persist_path: Optional[Path] = None, target: Optional[str] = None):
        self.persist_path = persist_path
        self.target = target
        self.rules: dict[str, Rule] = {}
        if persist_path and persist_path.exists():
            self._load()

    # ---------- writers ----------

    def add(self, rule: Rule) -> str:
        if rule.category not in CATEGORIES:
            raise ValueError(f"Unknown category: {rule.category!r}; must be one of {CATEGORIES}")
        if not rule.created_utc:
            rule.created_utc = datetime.now(timezone.utc).isoformat()
        if rule.rule_id in self.rules:
            existing = self.rules[rule.rule_id]
            existing.update_evidence(success=True)
            return existing.rule_id
        rule.last_updated_utc = rule.created_utc
        self.rules[rule.rule_id] = rule
        self._save()
        return rule.rule_id

    def add_negative(self, smiles: str, *, reason: str = "low_score",
                     context_scaffolds: Optional[list[str]] = None) -> str:
        rid = f"neg::{smiles}"
        # Initial state: the observation that motivated this negative rule IS a
        # successful prediction (the molecule WAS bad). So successes=1, not 0.
        return self.add(Rule(
            rule_id=rid,
            category=NEGATIVE,
            description=f"Avoid SMILES {smiles}: {reason}",
            pattern={"smiles": smiles},
            applicable_context=context_scaffolds or [],
            evidence_strength=1.0,
            observations=1,
            successes=1,
        ))

    def add_positive_transformation(self, *, edit: dict, parent_smiles: str,
                                     child_smiles: str, property_delta: float,
                                     context_scaffolds: Optional[list[str]] = None,
                                     source_round: Optional[int] = None) -> str:
        rid = f"pos::{parent_smiles}::{edit.get('operation','edit')}::{edit.get('arguments',{}).get('fragment_smiles','?')}::{edit.get('arguments',{}).get('atom_index','?')}"
        return self.add(Rule(
            rule_id=rid,
            category=POSITIVE,
            description=(f"{edit.get('operation','edit')} fragment "
                        f"{edit.get('arguments',{}).get('fragment_smiles','?')} at site "
                        f"{edit.get('arguments',{}).get('atom_index','?')} on parent "
                        f"{parent_smiles} produced +{property_delta:.4f} property gain"),
            pattern=edit,
            applicable_context=context_scaffolds or [],
            evidence_strength=min(1.0, max(0.0, property_delta / 0.05)),
            observations=1,
            successes=1,
            source_round=source_round,
            source_smiles=child_smiles,
            parent_smiles=parent_smiles,
        ))

    def add_context(self, *, scaffold_class: str, description: str,
                     related_rules: Optional[list[str]] = None) -> str:
        rid = f"ctx::{scaffold_class}"
        # related_rules stays in pattern (not a dataclass field) to avoid
        # schema bloat on persistence
        return self.add(Rule(
            rule_id=rid,
            category=CONTEXT,
            description=f"Context class {scaffold_class}: {description}",
            pattern={"scaffold_class": scaffold_class,
                     "related_rules": related_rules or []},
            applicable_context=[scaffold_class],
            evidence_strength=0.5,  # context tags start neutral
            observations=1,
        ))

    # ---------- readers ----------

    def retrieve(self, *, parent_smiles: Optional[str] = None,
                 scaffold_class: Optional[str] = None,
                 category: Optional[str] = None,
                 top_k: int = 20) -> list[Rule]:
        """Return rules relevant to `context`, sorted by evidence * relevance."""
        results = []
        for rule in self.rules.values():
            if category and rule.category != category:
                continue
            relevance = 1.0
            if parent_smiles and rule.parent_smiles and rule.parent_smiles != parent_smiles:
                relevance *= 0.5
            if scaffold_class and rule.applicable_context:
                if scaffold_class not in rule.applicable_context:
                    relevance *= 0.3
            results.append((rule, rule.evidence_strength * relevance))
        results.sort(key=lambda x: x[1], reverse=True)
        return [r for r, _ in results[:top_k]]

    def by_category(self, category: str) -> list[Rule]:
        return [r for r in self.rules.values() if r.category == category]

    def counts(self) -> dict[str, int]:
        out = {c: 0 for c in CATEGORIES}
        for r in self.rules.values():
            out[r.category] = out.get(r.category, 0) + 1
        return out

    def format_for_prompt(self, *, context: Optional[dict] = None,
                          max_chars: int = 2000) -> str:
        """Build a compact text block distinguishing all four categories."""
        if context is None:
            context = {}
        rules = self.retrieve(parent_smiles=context.get("parent_smiles"),
                              scaffold_class=context.get("scaffold_class"),
                              top_k=20)
        sections: dict[str, list[str]] = {c: [] for c in CATEGORIES}
        for r in rules:
            line = f"  [{r.evidence_strength:.2f}|n={r.observations}] {r.description}"
            if len(line) > 200:
                line = line[:197] + "..."
            sections[r.category].append(line)
        out = ["4-category memory:"]
        for cat in CATEGORIES:
            out.append(f"--- {cat} ({len(sections[cat])}) ---")
            out.extend(sections[cat][:5])
        text = "\n".join(out)
        return text[:max_chars]

    # ---------- persistence ----------

    def _save(self) -> None:
        if not self.persist_path:
            return
        try:
            self.persist_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "schema_version": 1,
                "target": self.target,
                "saved_utc": datetime.now(timezone.utc).isoformat(),
                "rules": {rid: asdict(r) for rid, r in self.rules.items()},
            }
            self.persist_path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as exc:
            print(f"[WARN] failed to persist rule memory: {exc}")

    def _load(self) -> None:
        try:
            payload = json.loads(self.persist_path.read_text(encoding="utf-8"))
            for rid, raw in payload.get("rules", {}).items():
                self.rules[rid] = Rule(**raw)
        except Exception as exc:
            print(f"[WARN] failed to load rule memory: {exc}")