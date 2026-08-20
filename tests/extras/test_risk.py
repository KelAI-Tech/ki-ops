from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from ki_ops.intents import TargetIntent, load_sod_positions_csv, load_target_intents_csv
from ki_ops.models import Holding
from ki_ops.portfolio import portfolio_from_holdings
from ki_ops.extras.risk import (
    SecurityRecord,
    build_risk_snapshot,
    load_security_master_csv,
    snapshot_book,
    universe_from_records,
)

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples"
EXTRAS = EXAMPLES / "extras"


def _u(*rows: SecurityRecord):
    return universe_from_records(rows)


def test_two_name_gmv_weights_and_beta():
    """Long $1k beta 1.0 vs short $1k beta 2.0 on $2k GMV."""
    book = snapshot_book(
        portfolio_from_holdings(
            [Holding("AAPL", 100, 10), Holding("TSLA", -50, 20)],
            cash=500,
        ),
        _u(
            SecurityRecord(
                "AAPL",
                sector="Information Technology",
                industry="Technology Hardware",
                betas={"spx": 1},
                factors={"value": Decimal("0.5")},
            ),
            SecurityRecord(
                "TSLA",
                sector="Consumer Discretionary",
                industry="Automobiles",
                betas={"spx": 2},
                factors={"value": Decimal("-1")},
            ),
        ),
    )
    assert book.gmv == Decimal("2000")
    assert book.nmv == Decimal("0")
    assert book.nav == Decimal("500")
    assert book.net_pct_gmv == Decimal("0")
    assert book.gross_pct_nav == Decimal("4")
    assert book.n_long == 1 and book.n_short == 1
    assert book.dollar_beta["spx"] == Decimal("-1000")
    assert book.beta_gmv["spx"] == Decimal("-0.5")
    assert book.beta_nav["spx"] == Decimal("-2")

    value = {b.id: b for b in book.factors}["value"]
    assert value.long == Decimal("0.25")
    assert value.short == Decimal("0.5")
    assert value.net == Decimal("0.75")

    sectors = {b.name: b for b in book.sectors}
    assert sectors["Information Technology"].net == Decimal("0.5")
    assert sectors["Consumer Discretionary"].net == Decimal("-0.5")


def test_missing_universe_flagged_and_bucketed_unknown():
    book = snapshot_book(
        portfolio_from_holdings([Holding("ZZZ", 10, 10)], cash=0),
        _u(SecurityRecord("AAPL", sector="Information Technology", betas={"spx": 1})),
    )
    assert book.missing_universe == ("ZZZ",)
    assert book.sectors[0].name == "Unknown"
    assert book.beta_gmv["spx"] == Decimal("0")


def test_security_master_csv_parses_prefixes():
    uni = load_security_master_csv(EXTRAS / "security_master.csv")
    aapl = uni.get("AAPL")
    assert aapl.sector == "Information Technology"
    assert aapl.industry == "Technology Hardware"
    assert aapl.betas["spx"] == Decimal("1.20")
    assert aapl.factors["value"] == Decimal("-0.40")
    assert "value" in uni.factor_ids
    assert uni.beta_ids[0] == "spx"


def test_example_sod_snapshot_is_dollar_neutral():
    sod = load_sod_positions_csv(EXAMPLES / "sod_positions.csv")
    uni = load_security_master_csv(EXTRAS / "security_master.csv")
    book = snapshot_book(sod, uni)
    assert book.nmv == Decimal("0")
    assert book.net_pct_gmv == Decimal("0")
    assert not book.missing_universe
    assert book.n_long == 6 and book.n_short == 4
    names = {p.symbol for p in book.positions}
    assert names >= {"AAPL", "TSLA", "XOM"}


def test_sod_vs_target_delta_includes_new_shorts():
    sod = load_sod_positions_csv(EXAMPLES / "sod_positions.csv")
    targets = load_target_intents_csv(EXAMPLES / "target_intents.csv")
    uni = load_security_master_csv(EXTRAS / "security_master.csv")
    snap = build_risk_snapshot(sod, uni, targets=targets)
    assert snap.projected is not None
    payload = snap.to_dict()
    current_names = {p["symbol"] for p in payload["current"]["positions"]}
    projected_names = {p["symbol"] for p in payload["projected"]["positions"]}
    assert "COIN" not in current_names
    assert "COIN" in projected_names
    assert "XOM" in current_names
    assert "XOM" not in projected_names
    assert payload["delta"]["n_short"] == 1  # 4 -> 5
    assert any(s["id"] == "Semiconductors" for s in payload["delta"]["industries"])


def test_flatten_missing_targets_exits_unmentioned():
    sod = portfolio_from_holdings([Holding("AAPL", 10, 10), Holding("MSFT", 5, 10)], cash=0)
    uni = _u(
        SecurityRecord("AAPL", sector="IT", industry="HW"),
        SecurityRecord("MSFT", sector="IT", industry="SW"),
    )
    snap = build_risk_snapshot(sod, uni, targets=[TargetIntent("AAPL", 10, 10)])
    assert {p.symbol for p in snap.current.positions} == {"AAPL", "MSFT"}
    assert {p.symbol for p in snap.projected.positions} == {"AAPL"}
