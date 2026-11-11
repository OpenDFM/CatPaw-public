from __future__ import annotations

from src.tools.runner_tools import build_completion_assessment
from src.utils.results import normalize_completion_policy


def make_summary(*, force: float = 0.07, reached_nelm: bool = False) -> dict:
    return {
        "converged": False,
        "final_energy_eV": -10.0,
        "max_force_eVA": force,
        "reached_nelm": reached_nelm,
        "errors": ["Ionic relaxation did not reach required accuracy"],
        "min_bond_length_A": 1.02,
        "total_magnetization": 50.0,
    }


def test_strict_policy_does_not_accept_partial_candidates() -> None:
    snapshot = {
        "all_terminal": True,
        "all_converged": False,
        "results": {
            "candidate_a": "CONVERGED",
            "candidate_b": "STALLED",
        },
    }
    summaries = {
        "candidate_a": {**make_summary(force=0.04), "converged": True},
        "candidate_b": make_summary(force=0.08),
    }

    result = build_completion_assessment(
        snapshot,
        summaries,
        {},
        normalize_completion_policy("strict"),
        node_id="ni_h2_rep",
    )

    assert result["objective_satisfied"] is False
    assert result["partial_candidate_policy_applied"] is False


def test_demonstration_policy_accepts_one_representative() -> None:
    snapshot = {
        "all_terminal": True,
        "all_converged": False,
        "results": {
            "candidate_a": "CONVERGED",
            "candidate_b": "STALLED",
        },
    }
    summaries = {
        "candidate_a": {**make_summary(force=0.04), "converged": True},
        "candidate_b": make_summary(force=0.20),
    }

    result = build_completion_assessment(
        snapshot,
        summaries,
        {},
        normalize_completion_policy("demonstration"),
        node_id="pdni_h1_rep",
    )

    assert result["objective_satisfied"] is True
    assert result["usable_calculations"][0]["quality_tier"] == "strictly_converged"
    assert result["excluded_calculations"][0]["logical_subdir"] == "candidate_b"


def test_demonstration_policy_rejects_scf_failure() -> None:
    snapshot = {
        "all_terminal": True,
        "all_converged": False,
        "results": {"candidate": "STALLED"},
    }

    result = build_completion_assessment(
        snapshot,
        {"candidate": make_summary(force=0.04, reached_nelm=True)},
        {},
        normalize_completion_policy("demonstration"),
        node_id="ni_h2_rep",
    )

    assert result["objective_satisfied"] is False
    assert "reached NELM" in result["excluded_calculations"][0]["reasons"][0]


def test_configured_demo_convergence_is_not_labeled_strict_above_force_limit() -> None:
    snapshot = {
        "all_terminal": True,
        "all_converged": True,
        "results": {"candidate": "CONVERGED"},
    }
    result = build_completion_assessment(
        snapshot,
        {"candidate": {**make_summary(force=0.08), "converged": True}},
        {},
        normalize_completion_policy("demonstration"),
        node_id="ni_h3_rep",
    )

    assert result["objective_satisfied"] is True
    assert result["usable_calculations"][0]["quality_tier"] == "demonstration_accepted"
