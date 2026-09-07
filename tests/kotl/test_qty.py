"""Tests for KOTL quantity rules (offline)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from ki_ops.kotl.enums import OrderStatus
from ki_ops.kotl.qty import derive_status, flex_status_label, is_flat, leaves_qty, side_sign, signed_qty


@pytest.mark.parametrize(
    ("side", "unsigned", "signed"),
    [
        ("BUY", 123, Decimal("123")),
        ("SELL", 123, Decimal("-123")),
        ("SHORT", 50, Decimal("-50")),
    ],
)
def test_signed_qty(side, unsigned, signed):
    assert signed_qty(side, unsigned) == signed


def test_side_sign():
    assert side_sign("buy") == Decimal("1")
    assert side_sign("SELL") == Decimal("-1")


def test_flex_status_label():
    assert flex_status_label(5) == "FILLED"
    assert flex_status_label("3") == "CANCELLED"
    assert flex_status_label("PARTIALLY_FILLED") == "PARTIALLY_FILLED"
    assert flex_status_label(None) is None


@pytest.mark.parametrize(
    ("sent", "filled", "flex_status", "expected"),
    [
        (Decimal("100"), Decimal("0"), 2, OrderStatus.OPEN),
        (Decimal("100"), Decimal("100"), 5, OrderStatus.DONE),
        (Decimal("100"), Decimal("40"), 4, OrderStatus.PARTIAL),
        (Decimal("100"), Decimal("40"), 3, OrderStatus.CANCELLED),
    ],
)
def test_derive_status_with_enum_ints(sent, filled, flex_status, expected):
    assert derive_status(sent, filled, flex_status=flex_status) == expected


@pytest.mark.parametrize(
    ("sent", "filled", "flex_status", "expected"),
    [
        (Decimal("100"), Decimal("0"), None, OrderStatus.OPEN),
        (Decimal("100"), Decimal("40"), None, OrderStatus.PARTIAL),
        (Decimal("100"), Decimal("100"), None, OrderStatus.DONE),
        (Decimal("-100"), Decimal("-60"), None, OrderStatus.PARTIAL),
        (Decimal("-100"), Decimal("-100"), None, OrderStatus.DONE),
        (Decimal("100"), Decimal("40"), "CANCELLED", OrderStatus.CANCELLED),
    ],
)
def test_derive_status(sent, filled, flex_status, expected):
    assert derive_status(sent, filled, flex_status=flex_status) == expected


def test_leaves_qty_open_and_done():
    assert leaves_qty(Decimal("100"), Decimal("0"), OrderStatus.OPEN) == Decimal("100")
    assert leaves_qty(Decimal("100"), Decimal("60"), OrderStatus.PARTIAL) == Decimal("40")
    assert leaves_qty(Decimal("100"), Decimal("100"), OrderStatus.DONE) == Decimal("0")
    assert leaves_qty(Decimal("-100"), Decimal("-40"), OrderStatus.PARTIAL) == Decimal("-60")


def test_leaves_qty_cancelled_zeros_remaining_work():
    assert leaves_qty(Decimal("100"), Decimal("40"), OrderStatus.CANCELLED) == Decimal("0")


def test_is_flat():
    assert is_flat([Decimal("0"), Decimal("0")])
    assert is_flat([Decimal("0"), Decimal("-0")])
    assert not is_flat([Decimal("5"), Decimal("0")])
    assert not is_flat([Decimal("10"), Decimal("-10")])  # abs sum, not net
    assert is_flat([Decimal("0.5")], tolerance=Decimal("1"))
