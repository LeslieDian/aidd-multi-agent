"""Regression tests for prediction_error tracking."""
from agents.rule_memory import EVIDENCE, RuleStore


def test_add_prediction_error_creates_evidence_rule():
    store = RuleStore()
    rid = store.add_prediction_error(
        parent_smiles="Oc1ccccc1",
        child_smiles="Nc1ccc(O)cc1",
        predicted_delta=0.02,
        observed_delta=0.005,
        context_scaffolds=["phenol"],
    )
    assert rid in store.rules
    rule = store.rules[rid]
    assert rule.category == EVIDENCE
    assert rule.pattern["parent_smiles"] == "Oc1ccccc1"
    assert rule.pattern["child_smiles"] == "Nc1ccc(O)cc1"
    assert rule.pattern["direction"] == "under"  # predicted > observed
    assert rule.evidence_strength > 0
    assert "calibration error" in rule.description


def test_add_prediction_error_over_direction():
    store = RuleStore()
    rid = store.add_prediction_error(
        parent_smiles="Oc1ccccc1",
        child_smiles="COc1ccccc1",
        predicted_delta=0.005,
        observed_delta=0.02,
    )
    rule = store.rules[rid]
    assert rule.pattern["direction"] == "over"


def test_add_prediction_error_same_pair_accumulates():
    """Calling with the same parent+child+direction updates the same rule."""
    store = RuleStore()
    rid1 = store.add_prediction_error(
        parent_smiles="Oc1ccccc1", child_smiles="COc1ccccc1",
        predicted_delta=0.02, observed_delta=0.01,
    )
    rid2 = store.add_prediction_error(
        parent_smiles="Oc1ccccc1", child_smiles="COc1ccccc1",
        predicted_delta=0.025, observed_delta=0.012,
    )
    assert rid1 == rid2
    assert store.rules[rid1].observations == 2
    assert store.rules[rid1].successes == 2


def test_evidence_category_distinct_from_positive_negative():
    store = RuleStore()
    store.add_prediction_error(
        parent_smiles="Oc1ccccc1", child_smiles="COc1ccccc1",
        predicted_delta=0.02, observed_delta=0.005,
    )
    store.add_negative("COc1ccccc1", reason="smells_bad")
    cats = {r.category for r in store.rules.values()}
    assert EVIDENCE in cats
    assert "negative_constraint" in cats
    assert cats == {EVIDENCE, "negative_constraint"}


def test_prediction_error_format_for_prompt_includes_insufficient():
    store = RuleStore()
    store.add_prediction_error(
        parent_smiles="Oc1ccccc1", child_smiles="COc1ccccc1",
        predicted_delta=0.02, observed_delta=0.005,
    )
    text = store.format_for_prompt()
    assert EVIDENCE in text
    assert "insufficient" in text.lower() or "prediction" in text.lower()