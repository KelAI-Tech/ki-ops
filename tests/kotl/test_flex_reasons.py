"""Classification of FlexTrade CreateOrders rejection messages.

The warning-family strings below are verbatim from the Flex PROD gateway
(2026-09-15/16 submits): exposure-calc *warnings* — missing analytics inputs
that fail the rule only because the account tolerates 0.00% calc errors —
must be distinguished from true rejections.
"""

from __future__ import annotations

import pytest

from ki_ops.kotl.flex_reasons import (
    CALC_WARNING,
    TRUE_REJECTION,
    is_exposure_calc_warning,
    rejection_kind,
    rejection_reason_from_result,
)

CALC_WARNING_REASONS = [
    "Error: Calc failed for 13.9187% (298/2141) of securities. See exception report email.",
    # The gateway sometimes doubles the message on fund-split parents.
    "Error: Calc failed for 13.9187% (298/2141) of securities. See exception report email.; "
    "Error: Calc failed for 13.9187% (298/2141) of securities. See exception report email.",
    "Error: Missing Beta for security MOMO.US",
    "Error: Missing Beta for security PVLA.US; Error: PVLA.US has invalid Avg Volume (90D) = NaN",
    "Error: Missing ExposurePrice for security MPW.US; Error: Missing ExposurePrice for "
    "security MPW.US; Error: MPW.US has invalid Avg Volume (90D) = NaN",
]

TRUE_REJECTION_REASONS = [
    "create rejected",
    "risk: restricted list",
    "Error: account not permissioned for short sales",
    # A calc warning mixed with anything unrecognized stays a true rejection.
    "Error: Missing Beta for security X.US; Error: account not permissioned",
]


@pytest.mark.parametrize("reason", CALC_WARNING_REASONS)
def test_calc_warning_reasons(reason):
    assert is_exposure_calc_warning(reason)
    assert rejection_kind(reason) == CALC_WARNING


@pytest.mark.parametrize("reason", TRUE_REJECTION_REASONS)
def test_true_rejection_reasons(reason):
    assert not is_exposure_calc_warning(reason)
    assert rejection_kind(reason) == TRUE_REJECTION


def test_no_reason_is_no_kind():
    assert not is_exposure_calc_warning(None)
    assert not is_exposure_calc_warning("")
    assert rejection_kind(None) is None
    assert rejection_kind("") is None


def test_reason_from_result_success_is_none():
    assert rejection_reason_from_result({"orderId": "X-1", "success": True}) is None
    assert rejection_reason_from_result({"orderId": "X-1"}) is None


def test_reason_from_result_prefers_description():
    result = {
        "orderId": "X-1",
        "success": False,
        "description": "Error: Missing Beta for security A.US",
        "issues": ["ignored"],
    }
    assert rejection_reason_from_result(result) == "Error: Missing Beta for security A.US"


def test_reason_from_result_falls_back_to_issues_then_bare():
    result = {"orderId": "X-1", "success": False, "issues": ["risk: a", "risk: b"]}
    assert rejection_reason_from_result(result) == "risk: a; risk: b"
    assert rejection_reason_from_result({"orderId": "X-1", "success": False}) == "create rejected"
