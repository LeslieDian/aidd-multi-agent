"""Tests for compute_calibration_metrics (Phase 4.4)."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from agents.rule_memory import EVIDENCE, RuleStore
from agents.agent_metrics import (
    LEARNING_DELTA_THRESHOLD, compute_agent_metrics, compute_calibration_metrics,
)


def test_empty_rule_store_returns_zero_block():
    """None / empty RuleStore -> zero-filled calibration block."""
    assert compute_calibration_metrics(None)["n_evidence_rules"] == 0
    store = RuleStore()
    out = compute_calibration_metrics(store)
    assert out["n_evidence_rules"] == 0
    assert out["mean_abs_error"] is None
    assert out["worst_pair"] is None


def test_single_under_claim():
    store = RuleStore()
    store.add_prediction_error(
        parent_smiles="Oc1ccccc1", child_smiles="COc1ccccc1",
        predicted_delta=0.020, observed_delta=0.005,
    )
    out = compute_calibration_metrics(store)
    assert out["n_evidence_rules"] == 1
    assert out["mean_abs_error"] == pytest.approx(0.015, abs=1e-4)
    assert out["by_direction"] == {"under": 1, "over": 0}
    assert out["under_claim_rate"] == 1.0
    assert out["over_claim_rate"] == 0.0
    assert out["worst_pair"]["parent_smiles"] == "Oc1ccccc1"
    assert out["worst_pair"]["child_smiles"] == "COc1ccccc1"
    assert out["worst_pair"]["abs_error"] == pytest.approx(0.015, abs=1e-4)


def test_single_over_claim():
    store = RuleStore()
    store.add_prediction_error(
        parent_smiles="Oc1ccccc1", child_smiles="COc1ccccc1",
        predicted_delta=0.005, observed_delta=0.025,
    )
    out = compute_calibration_metrics(store)
    assert out["by_direction"] == {"under": 0, "over": 1}
    assert out["over_claim_rate"] == 1.0
    assert out["worst_pair"]["direction"] == "over"


def test_accumulated_pair_uses_observations_log():
    """Three observations on the same parent-child pair -> 3 errors counted."""
    store = RuleStore()
    for predicted, observed in [(0.020, 0.005), (0.025, 0.012), (0.018, 0.006)]:
        store.add_prediction_error(
            parent_smiles="Oc1ccccc1", child_smiles="COc1ccccc1",
            predicted_delta=predicted, observed_delta=observed,
        )
    out = compute_calibration_metrics(store)
    assert out["n_evidence_rules"] == 1
    # Three observations with abs errors 0.015, 0.013, 0.012
    assert out["mean_abs_error"] == pytest.approx((0.015 + 0.013 + 0.012) / 3, abs=1e-4)
    assert out["max_abs_error"] == pytest.approx(0.015, abs=1e-4)
    # median of [0.012, 0.013, 0.015] is 0.013
    assert out["median_abs_error"] == pytest.approx(0.013, abs=1e-4)


def test_persistence_round_trip_preserves_log():
    """Persisting and reloading keeps observations_log so recalibration is exact."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "rules.json"
        s = RuleStore(persist_path=path, target="EGFR")
        s.add_prediction_error(parent_smiles="Oc1ccccc1", child_smiles="COc1ccccc1",
                                 predicted_delta=0.020, observed_delta=0.005)
        s.add_prediction_error(parent_smiles="Oc1ccccc1", child_smiles="CCOc1ccccc1",
                                 predicted_delta=0.040, observed_delta=0.060)
        out_before = compute_calibration_metrics(s)

        # Reload from disk
        s2 = RuleStore(persist_path=path, target="EGFR")
        out_after = compute_calibration_metrics(s2)
        assert out_before["mean_abs_error"] == out_after["mean_abs_error"]
        assert out_before["max_abs_error"] == out_after["max_abs_error"]
        assert out_before["n_evidence_rules"] == out_after["n_evidence_rules"]


def test_under_over_balance_lands_in_rates():
    store = RuleStore()
    store.add_prediction_error(parent_smiles="Oc1ccccc1", child_smiles="COc1ccccc1",
                                 predicted_delta=0.02, observed_delta=0.005)  # under
    store.add_prediction_error(parent_smiles="Oc1ccccc1", child_smiles="CCOc1ccccc1",
                                 predicted_delta=0.04, observed_delta=0.060)  # over
    out = compute_calibration_metrics(store)
    assert out["by_direction"] == {"under": 1, "over": 1}
    assert out["under_claim_rate"] == 0.5
    assert out["over_claim_rate"] == 0.5


def test_compute_agent_metrics_emits_calibration_block():
    """compute_agent_metrics with a rule_store -> the new calibration block
    is populated alongside curves/aggregates/verdict."""
    s = RuleStore()
    s.add_prediction_error(parent_smiles="Oc1ccccc1", child_smiles="COc1ccccc1",
                             predicted_delta=0.020, observed_delta=0.005)
    summaries = [{"round": 0, "n_total": 4, "n_valid": 3, "n_complete": 3,
                    "valid_ratio": 0.75, "n_unique_scaffolds": 2, "avg_admet": 0.7,
                    "best_vina": -3.5, "best_smiles": "X"}]
    m = compute_agent_metrics(summaries, [], {}, rule_store=s)
    assert "calibration" in m
    assert m["calibration"]["n_evidence_rules"] == 1
    assert m["calibration"]["mean_abs_error"] == pytest.approx(0.015, abs=1e-4)
    assert m["schema_version"] == 3


def test_compute_agent_metrics_without_rule_store_calibration_is_zero():
    """Backwards compatibility: callers that don't pass rule_store get the
    zero-filled calibration block (no KeyError, no crash)."""
    summaries = [{"round": 0, "n_total": 4, "n_valid": 3, "n_complete": 3,
                    "valid_ratio": 0.75, "n_unique_scaffolds": 2, "avg_admet": 0.7,
                    "best_vina": -3.5, "best_smiles": "X"}]
    m = compute_agent_metrics(summaries, [], {})
    assert m["calibration"]["n_evidence_rules"] == 0
    assert m["calibration"]["mean_abs_error"] is None
    assert "calibration_rationale" in m


def test_calibration_output_is_json_safe():
    """Even if a RuleRule has the worst_pair pointing at SMILES with weird
    characters, the output must be json.dumps-able."""
    s = RuleStore()
    s.add_prediction_error(parent_smiles="Oc1ccccc1", child_smiles="Cc1ccccc1",
                             predicted_delta=0.01, observed_delta=0.02)
    out = compute_calibration_metrics(s)
    json.dumps(out)  # must not raise