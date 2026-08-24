from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from ki_ops.audit import settings_hash, sha256_file, write_json_out
from ki_ops.checks.rules import check_net_exposure, check_tradability
from ki_ops.config import RiskManagementSettings
from ki_ops.engine import PreTradeEngine
from ki_ops.listing import ListingStatus, load_listing_status
from ki_ops.models import Holding, Order, Side
from ki_ops.portfolio import portfolio_from_holdings

TS = datetime(2026, 8, 6, 15, 0, tzinfo=timezone.utc)
AS_OF = date(2026, 8, 6)


def _settings(**kwargs) -> RiskManagementSettings:
    base = dict(
        max_position_size=Decimal("1000000"),
        max_portfolio_value=Decimal("1000000"),
        max_position_concentration=Decimal("1"),
        min_order_size=Decimal("1"),
        max_order_size=Decimal("1000000"),
        max_orders_per_minute=100000,
        max_turnover=Decimal("1"),
        max_net_exposure=Decimal("0.10"),
        enforce_market_hours=False,
        allow_shorts=True,
    )
    base.update(kwargs)
    return RiskManagementSettings(**base)


def test_listing_status_blocks_delisted_and_inactive():
    dead = ListingStatus("5029", "LWSN", "D", False, date(2011, 7, 6))
    live = ListingStatus("6347", "SCCO", "A", True, None)
    assert dead.is_tradable(AS_OF) is False
    assert live.is_tradable(AS_OF) is True
    findings = check_tradability(
        [Order("5029", Side.BUY, 10, 5, TS), Order("6347", Side.BUY, 10, 5, TS)],
        {"5029": dead, "6347": live},
        as_of=AS_OF,
    )
    assert [v.code for v in findings] == ["NOT_TRADABLE"]
    assert findings[0].symbol == "5029"
    assert findings[0].severity.value == "WARN"


def test_unknown_infocode_warns():
    findings = check_tradability(
        [Order("999", Side.BUY, 1, 1, TS)],
        {},
        as_of=AS_OF,
    )
    assert findings[0].code == "NOT_IN_SECURITY_MASTER"
    assert findings[0].severity.value == "WARN"


def test_load_lseg_master_marks_dead_row():
    path = Path(__file__).resolve().parents[1] / "examples" / "lseg_security_master.csv"
    if not path.is_file():
        return
    master = load_listing_status(path)
    assert master["5029"].is_tradable(AS_OF) is False
    assert master["6347"].is_tradable(AS_OF) is True


def test_net_exposure_blocks_skewed_projected_book():
    sod = portfolio_from_holdings(
        [Holding("AAA", 50, 100), Holding("BBB", -50, 100)],
        cash=0,
    )
    # Flatten the short → projected NMV = +10_000, GMV = 10_000, ratio = 1.
    orders = [Order("BBB", Side.BUY, 50, 100, TS)]
    findings = check_net_exposure(sod, orders, _settings(max_net_exposure=Decimal("0.10")))
    assert findings[0].code == "MAX_NET_EXPOSURE"
    result = PreTradeEngine(settings=_settings(max_net_exposure=Decimal("0.10"))).evaluate(sod, orders)
    assert result.allowed is False
    assert "MAX_NET_EXPOSURE" in {v.code for v in result.violations}
    assert result.projected_net_exposure == Decimal("1")


def test_untradable_ticket_is_dropped_rest_of_book_still_sends():
    dead = ListingStatus("5029", "LWSN", "D", False, date(2011, 7, 6))
    live = ListingStatus("6347", "SCCO", "A", True, None)
    sod = portfolio_from_holdings(
        [Holding("5029", 10, 5), Holding("6347", 10, 5)],
        cash=0,
    )
    orders = [
        Order("5029", Side.BUY, 2, 5, TS),
        Order("6347", Side.BUY, 2, 5, TS),
    ]
    result = PreTradeEngine(settings=_settings(max_net_exposure=Decimal("1"))).evaluate(
        sod,
        orders,
        listing={"5029": dead, "6347": live},
        as_of=AS_OF,
    )
    assert result.allowed is True
    assert [o.symbol for o in result.trade_intents] == ["6347"]
    assert "NOT_TRADABLE" in {w.code for w in result.warnings}
    assert result.warnings[0].symbol == "5029"


def test_json_out_and_hashes(tmp_path: Path):
    p = tmp_path / "a.csv"
    p.write_text("hello\n", encoding="utf-8")
    digest = sha256_file(p)
    assert digest and len(digest) == 64
    settings = _settings()
    h = settings_hash(settings)
    assert h == settings_hash(settings)
    dest = tmp_path / "out.json"
    write_json_out(dest, {"ok": True, "config_hash": h})
    payload = json.loads(dest.read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert payload["config_hash"] == h
