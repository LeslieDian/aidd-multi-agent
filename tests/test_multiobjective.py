from agents.evaluator import (
    _composite_score,
    _safety_gate_pass,
    assign_pareto_metadata,
)
from tools.admet_score import estimate_admet


def _candidate(vina, prop, risk, sa, safety=True):
    return {
        "evaluation_status": "complete",
        "validate": {"valid": True, "sa_score": sa},
        "admet": {"valid": True, "herg_risk_score": risk},
        "dock": {"valid": True, "score": vina},
        "property_score": prop,
        "composite_score": prop,
        "safety_gate_pass": safety,
    }


def test_continuous_herg_proxy_covers_tertiary_amine_and_lipophilicity():
    polar = estimate_admet("CCN(C)C")
    lipophilic = estimate_admet("CCCCCCCCN(C)C")
    assert polar["basic_nitrogen_count"] >= 1
    assert lipophilic["basic_nitrogen_count"] >= 1
    assert 0.0 <= polar["herg_risk_score"] < lipophilic["herg_risk_score"] <= 1.0


def test_aromatic_core_nitrogens_are_weaker_than_aliphatic_amine():
    quinazoline = estimate_admet("c1ccc2ncnc(N)c2c1")
    with_amine = estimate_admet("CN1CCC(CC1)c1ccc2ncnc(N)c2c1")
    assert quinazoline["basic_nitrogen_score"] < with_amine["basic_nitrogen_score"]
    assert quinazoline["herg_risk_score"] < with_amine["herg_risk_score"]


def test_composite_retains_precision_and_explicit_safety_penalty():
    val = {"valid": True, "sa_score": 2.0, "lipinski_pass": True}
    dock = {"valid": True, "score": -8.0}
    safer = {
        "valid": True, "admet_quality_score": 0.80001,
        "summary_score": 0.8, "herg_risk_score": 0.1,
    }
    slightly_better = {**safer, "admet_quality_score": 0.80002}
    risky = {**safer, "herg_risk_score": 0.9}
    first = _composite_score(val, safer, dock, {})
    second = _composite_score(val, slightly_better, dock, {})
    assert second > first
    assert len(str(first).split(".")[-1]) > 3
    assert _composite_score(val, risky, dock, {}) < first


def test_pareto_rank_preserves_real_tradeoffs():
    rows = [
        _candidate(-9.0, 0.7, 0.7, 3.0, safety=False),
        _candidate(-8.0, 0.9, 0.1, 2.0, safety=True),
        _candidate(-7.0, 0.6, 0.8, 5.0, safety=False),
    ]
    assign_pareto_metadata(rows, {})
    assert rows[0]["pareto_rank"] == 1  # best docking, worse safety/property
    assert rows[1]["pareto_rank"] == 1  # safer property tradeoff
    assert rows[2]["pareto_rank"] > 1   # dominated by row 1


def test_safety_gate_uses_continuous_risk_and_logp():
    cfg = {"pareto": {"safety": {"max_herg_risk_score": 0.5, "max_logp": 4.5}}}
    val = {"valid": True, "logp": 3.0}
    assert _safety_gate_pass(val, {"valid": True, "herg_risk_score": 0.4}, cfg)
    assert not _safety_gate_pass(val, {"valid": True, "herg_risk_score": 0.6}, cfg)
    assert not _safety_gate_pass(
        {"valid": True, "logp": 5.0},
        {"valid": True, "herg_risk_score": 0.2},
        cfg,
    )
