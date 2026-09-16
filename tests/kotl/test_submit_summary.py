"""Submit ops summary: stats, formatting, webhook resolution, delivery."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from ki_ops.kotl.submit_summary import (
    build_submit_stats,
    format_ops_summary,
    ops_summary_subject,
    post_ops_summary,
    resolve_ops_webhook,
)
from ki_ops.models import Holding, Order, Portfolio, Side

TS = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)


def _order(symbol: str, side: str, qty: str, px: str) -> Order:
    return Order(symbol, Side(side), Decimal(qty), Decimal(px), TS)


ORDERS = [
    _order("AAPL", "BUY", "100", "250"),  # $25,000
    _order("HOS", "SELL", "477", "8"),  # $3,816 — flattens most of the short
    _order("LDI", "SELL", "1881", "12"),  # $22,572 — flatten (not in target)
    _order("NEWCO", "BUY", "50", "40"),  # $2,000 — brand-new position
]
PAYLOADS = [
    {"symbol": "AAPL.US"},
    {"symbol": "HLX.US"},  # canonical rewrite of HOS
    {"symbol": "LDI.US"},
    {"symbol": "NEWCO.US"},
]
SOD = Portfolio(
    holdings={
        "AAPL": Holding("AAPL", Decimal("1000"), Decimal("250")),
        "HOS": Holding("HOS", Decimal("-892"), Decimal("8")),
        "LDI": Holding("LDI", Decimal("1881"), Decimal("12")),
    }
)
RESULTS = [
    {"orderId": "S-1", "success": True},
    {
        "orderId": "S-2",
        "success": False,
        "description": "Error: Calc failed for 13.9187% (298/2141) of securities. "
        "See exception report email.",
    },
    {"orderId": "S-3", "success": True},
    {"orderId": "S-4", "success": False},
]


def test_build_stats_flow_and_extremes():
    stats = build_submit_stats(
        orders=ORDERS, payloads=PAYLOADS, results=RESULTS, sod=SOD, unresolved_skipped=2
    )
    assert (stats["orders"], stats["buys"], stats["sells"]) == (4, 2, 2)
    assert stats["unresolved_skipped"] == 2
    assert stats["shares"]["net"] == Decimal("100") + 50 - 477 - 1881
    assert stats["notional"]["gross"] == Decimal("53388")
    assert stats["notional"]["net"] == Decimal("25000") + 2000 - 3816 - 22572

    assert stats["max_order_shares"]["symbol"] == "LDI.US"
    assert stats["max_order_notional"]["symbol"] == "AAPL.US"
    assert stats["max_order_notional"]["notional"] == Decimal("25000")
    # LDI sells 1881 of 1881 held (100%) > HOS 477 of 892 (~53%) > AAPL 10%.
    assert stats["max_sod_turnover"]["symbol"] == "LDI.US"
    assert stats["max_sod_turnover"]["turnover_pct"] == Decimal("100")
    # NEWCO has no SOD position: excluded from turnover, flagged as new.
    assert stats["largest_new_position"]["symbol"] == "NEWCO.US"
    assert [s["symbol"] for s in stats["top_orders_by_notional"][:2]] == [
        "AAPL.US",
        "LDI.US",
    ]

    assert stats["verdicts"] == {
        "submitted": 2,
        "rejected": 2,
        "calc_warnings": 1,
        "true_rejections": 1,
    }


def test_build_stats_without_results_or_sod():
    stats = build_submit_stats(orders=ORDERS, payloads=PAYLOADS)
    assert "verdicts" not in stats
    assert "max_sod_turnover" not in stats
    assert stats["max_order_shares"]["symbol"] == "LDI.US"


def test_format_and_subject():
    stats = build_submit_stats(
        orders=ORDERS, payloads=PAYLOADS, results=RESULTS, sod=SOD, unresolved_skipped=2
    )
    meta = {
        "trade_date": "2026-09-16",
        "env": "PROD",
        "no_route": True,
        "strategy_id": "strategy-x",
        "submit_id": "sub-1",
        "trade_file": "s3://bucket/trades.csv",
    }
    body = format_ops_summary(stats, meta=meta)
    assert "KOTL submit — 2026-09-16 PROD (no-route)" in body
    assert "strategy: strategy-x" in body
    assert "orders: 4 (2 buys / 2 sells)   unresolved skipped: 2" in body
    assert "verdicts: 2 submitted, 2 rejected (1 exposure-calc warnings, 1 true rejections)" in body
    assert "max order (shares): LDI.US SELL 1,881 ($22,572)" in body
    assert "max turnover vs SOD: LDI.US SELL 1,881 ($22,572) vs 1,881 held — 100%" in body
    assert "largest new position: NEWCO.US BUY 50 ($2,000)" in body
    assert "net -2,208" in body  # shares net
    assert "trade file: s3://bucket/trades.csv" in body

    subject = ops_summary_subject(stats, meta=meta)
    assert subject == (
        "KOTL submit PROD 2026-09-16 (no-route) — 2/4 submitted, "
        "1 calc-warnings, 1 REJECTED"
    )


def test_resolve_webhook_env_wins(monkeypatch):
    monkeypatch.setenv("KI_OPS_SLACK_WEBHOOK_URL", "https://hooks.slack.example/x")
    assert resolve_ops_webhook() == "https://hooks.slack.example/x"


def test_resolve_webhook_ssm_failure_is_none(monkeypatch, capsys):
    monkeypatch.delenv("KI_OPS_SLACK_WEBHOOK_URL", raising=False)
    monkeypatch.setenv("KI_OPS_SLACK_WEBHOOK_SSM_PARAM", "/nonexistent/param")

    class Boom:
        def client(self, *_a, **_k):
            raise RuntimeError("no aws here")

    monkeypatch.setitem(__import__("sys").modules, "boto3", Boom())
    assert resolve_ops_webhook() is None
    assert "ops webhook lookup failed" in capsys.readouterr().out


def test_post_ops_summary_best_effort(monkeypatch, capsys):
    sent = []
    monkeypatch.setenv("KI_OPS_SLACK_WEBHOOK_URL", "https://hooks.slack.example/x")
    monkeypatch.setattr(
        "ki_ops.kotl.submit_summary.send_slack",
        lambda *, webhook_url, subject, body: sent.append((webhook_url, subject, body)),
    )
    assert post_ops_summary(subject="s", body="b") is True
    assert sent == [("https://hooks.slack.example/x", "s", "b")]

    def boom(**_kw):
        raise RuntimeError("slack down")

    monkeypatch.setattr("ki_ops.kotl.submit_summary.send_slack", boom)
    assert post_ops_summary(subject="s", body="b") is False
    assert "delivery failed (ignored)" in capsys.readouterr().out


def test_post_ops_summary_no_webhook(monkeypatch, capsys):
    monkeypatch.delenv("KI_OPS_SLACK_WEBHOOK_URL", raising=False)
    monkeypatch.setattr("ki_ops.kotl.submit_summary.resolve_ops_webhook", lambda: None)
    assert post_ops_summary(subject="s", body="b") is False
    assert "not posted" in capsys.readouterr().out
