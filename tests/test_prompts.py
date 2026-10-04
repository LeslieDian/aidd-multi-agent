"""tests/test_prompts.py - Unit tests for agents.prompts registry."""
from __future__ import annotations

import pytest

from agents.prompts import DEFAULT_TEMPLATES, load, render


class TestLoad:
    def test_known_role(self):
        tmpl = load("qed")
        assert "{n}" in tmpl
        assert "{focus}" in tmpl

    def test_expert_role(self):
        assert "PROPERTY EXPERT" in load("prompt_qed_expert")
        assert "DOCKING EXPERT" in load("prompt_vina_expert")
        assert "SA EXPERT" in load("prompt_sa_expert")
        assert "EXPLOIT MODE" in load("prompt_exploit_expert")

    def test_unknown_role_falls_back_to_default(self):
        tmpl = load("this_role_does_not_exist")
        assert "{n}" in tmpl

    def test_default_templates_has_all_three_generators(self):
        # The three heterogeneous generator roles
        for role in ("qed", "vina", "synth"):
            assert role in DEFAULT_TEMPLATES
            assert "{n}" in DEFAULT_TEMPLATES[role]

    def test_default_templates_has_all_three_judges(self):
        # Phase 4.7: judge_* templates close the 5-role contract.
        for role in ("judge_property", "judge_docking", "judge_synthesis"):
            assert role in DEFAULT_TEMPLATES, f"{role} should be registered"
            tmpl = DEFAULT_TEMPLATES[role]
            # Judges should NOT ask the model to generate SMILES; they should
            # ask for an actionable next-round focus.
            assert "smiles_list" not in tmpl.lower(), (
                f"{role} prompt must not request new SMILES; it is a reviewer."
            )
            assert "focus" in tmpl
            assert "confidence" in tmpl


class TestRender:
    def test_basic_render(self):
        out = render("qed", {"n": "5", "focus": "lower hERG", "weakness": "high MW"})
        assert "5" in out
        assert "lower hERG" in out
        assert "high MW" in out

    def test_missing_placeholder_rendered_empty(self):
        out = render("qed", {"n": "3"})
        # focus / weakness / memory / parents_block / failed_prompt all empty
        assert "3" in out
        # ensure no KeyError, no curly-brace artifacts in known positions
        assert "Generate 3" in out

    def test_expert_render(self):
        out = render("prompt_vina_expert", {"n": "5"})
        assert "DOCKING EXPERT" in out
        assert "5" in out

    def test_judge_render_works_without_placeholders(self):
        # Judge templates should not require any placeholders to be passed.
        for role in ("judge_property", "judge_docking", "judge_synthesis"):
            out = render(role, {})
            assert "JSON" in out or "json" in out
            assert "focus" in out