from copy import deepcopy

import pytest

from scripts.eval_v4a_banks import EvalFailure, _audit_bank_values


def test_gate_separates_safe_missing_coverage_from_invalid_valuation() -> None:
    data = {
        "disclosure": {"schema_version": "v4a.3", "tbvps_bridge": {}},
        "diagnostics": {"cet1_buffer_bps": "100"},
        "valuation": {"price_usd": "50", "primary_tbvps_basis": "unavailable", "sensitivity_matrix": []},
    }
    coverage = _audit_bank_values("TEST", data)
    assert not coverage["tbvps"] and not coverage["sensitivity"]
    invalid = deepcopy(data)
    invalid["valuation"]["p_to_tbv_primary"] = "1.2"
    with pytest.raises(EvalFailure, match="unverified/non-positive"):
        _audit_bank_values("TEST", invalid)
    negative = deepcopy(data)
    negative["disclosure"]["tbvps_bridge"] = {"tbvps_reported_usd": "-3", "tbvps_reported_verified": True}
    negative["valuation"]["primary_tbvps_basis"] = "reported_market_convention"
    assert not _audit_bank_values("TEST", negative)["tbvps"]
    negative["valuation"]["p_to_tbv_primary"] = "-16.67"
    with pytest.raises(EvalFailure, match="unverified/non-positive"):
        _audit_bank_values("TEST", negative)


def test_gate_uses_sec_stt_reference() -> None:
    data = {
        "disclosure": {"schema_version": "v4a.3", "period_end_date": "2026-09-30",
                       "rotce_q_verified": True, "rotce_q_reported_pct": "25.1",
                       "tbvps_bridge": {"tbvps_reported_verified": True, "tbvps_reported_usd": "59.69"}},
        "diagnostics": {"rotce_q_pct": "25.1"},
        "valuation": {"price_usd": "120", "primary_tbvps_basis": "reported_market_convention",
                      "p_to_tbv_primary": "2.01", "implied_rotce_pct": "17.07",
                      "current_rotce_pct": "25.1", "justified_p_to_tbv_at_current_rotce": "3.16",
                      "rotce_spread_vs_implied_pp": "8.03", "sensitivity_matrix": []},
    }
    coverage = _audit_bank_values("STT", data)
    assert coverage["reference_status"] == "matched" and not coverage["sensitivity"]
    data["disclosure"]["tbvps_bridge"]["tbvps_reported_usd"] = "17"
    with pytest.raises(EvalFailure, match="SEC checkpoint"):
        _audit_bank_values("STT", data)


def test_gate_accepts_negative_gordon_valuation_with_verified_rotce() -> None:
    data = {"disclosure": {"schema_version": "v4a.3", "rotce_q_verified": True,
                            "rotce_q_reported_pct": "-0.5", "tbvps_bridge": {}},
            "diagnostics": {"rotce_q_pct": "-0.5"},
            "valuation": {"primary_tbvps_basis": "unavailable", "current_rotce_pct": "-0.5",
                          "justified_p_to_tbv_at_current_rotce": "-0.5", "sensitivity_matrix": []}}
    assert not _audit_bank_values("TEST", data)["market_valuation"]
