"""Ops summary for a KOTL submit: order-flow stats + Slack #ops delivery.

Mirrors the ``ki_ops_gate`` ops notification (kelaidata
``lseg_strategy_pipeline_dag._notify_ops``): a bold subject plus a fenced
text body posted to the shared #ops incoming webhook. The webhook resolves
exactly like the gate's — a directly exported ``KI_OPS_SLACK_WEBHOOK_URL``
wins, else the SSM SecureString named by ``KI_OPS_SLACK_WEBHOOK_SSM_PARAM``
(default ``/kelaidata/slack/ops-webhook``). Delivery is best-effort: a Slack
failure must never fail a submit whose orders already went out.

The stats are alert-oriented: sizes and exposure an operator should eyeball
after every submit — the largest single orders (shares / notional), the
order-flow net (a big net says the book is drifting directional), the name
with the highest turnover vs its SOD position (flatten/flip moves), and the
largest brand-new position.
"""

from __future__ import annotations

import os
from decimal import Decimal
from typing import Any, Sequence

from ki_ops.extras.notify import ENV_SLACK_WEBHOOK, send_slack
from ki_ops.kotl.flex_reasons import is_exposure_calc_warning, rejection_reason_from_result
from ki_ops.models import Portfolio, Side

ENV_SLACK_WEBHOOK_SSM = "KI_OPS_SLACK_WEBHOOK_SSM_PARAM"
DEFAULT_SLACK_WEBHOOK_SSM_PARAM = "/kelaidata/slack/ops-webhook"

TOP_ORDERS_SHOWN = 5


def _order_stat(symbol: str, order) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "side": order.side.value,
        "quantity": abs(order.quantity),
        "notional": order.notional,
    }


def build_submit_stats(
    *,
    orders: Sequence,
    payloads: Sequence[dict],
    results: Sequence[dict] | None = None,
    sod: Portfolio | None = None,
    unresolved_skipped: int = 0,
) -> dict[str, Any]:
    """Order-flow stats for the ops summary.

    *orders* (``ki_ops.models.Order``, trade-time ``limit_price``) and
    *payloads* are positionally aligned; the payload carries the Flex symbol
    for display while the order's symbol keys the SOD book. *results* (the
    CreateOrders adapter output, also aligned) adds the gateway verdicts;
    *sod* adds turnover-vs-position and new-position stats.
    """
    zero = Decimal("0")
    buy_shares = sell_shares = zero
    buy_notional = sell_notional = zero
    per_order: list[tuple[str, Any]] = []  # (display symbol, order)
    for i, order in enumerate(orders):
        symbol = str(payloads[i].get("symbol") or order.symbol) if i < len(payloads) else order.symbol
        per_order.append((symbol, order))
        if order.side is Side.BUY:
            buy_shares += abs(order.quantity)
            buy_notional += order.notional
        else:
            sell_shares += abs(order.quantity)
            sell_notional += order.notional

    stats: dict[str, Any] = {
        "orders": len(orders),
        "buys": sum(1 for _, o in per_order if o.side is Side.BUY),
        "sells": sum(1 for _, o in per_order if o.side is Side.SELL),
        "unresolved_skipped": int(unresolved_skipped),
        "shares": {
            "buy": buy_shares,
            "sell": sell_shares,
            "net": buy_shares - sell_shares,
        },
        "notional": {
            "gross": buy_notional + sell_notional,
            "buy": buy_notional,
            "sell": sell_notional,
            "net": buy_notional - sell_notional,
        },
    }

    if per_order:
        by_shares = max(per_order, key=lambda so: abs(so[1].quantity))
        by_notional = max(per_order, key=lambda so: so[1].notional)
        stats["max_order_shares"] = _order_stat(by_shares[0], by_shares[1])
        stats["max_order_notional"] = _order_stat(by_notional[0], by_notional[1])
        stats["top_orders_by_notional"] = [
            _order_stat(sym, o)
            for sym, o in sorted(per_order, key=lambda so: so[1].notional, reverse=True)[
                :TOP_ORDERS_SHOWN
            ]
        ]

    if sod is not None and per_order:
        # Turnover vs the SOD position: |trade| / |held| — flags flatten and
        # flip moves. New positions (nothing held) are reported separately.
        max_turnover: tuple[Decimal, str, Any, Decimal] | None = None
        largest_new: tuple[str, Any] | None = None
        for symbol, order in per_order:
            held = sod.qty(order.symbol)
            if held == 0:
                if largest_new is None or order.notional > largest_new[1].notional:
                    largest_new = (symbol, order)
                continue
            ratio = abs(order.quantity) / abs(held)
            if max_turnover is None or ratio > max_turnover[0]:
                max_turnover = (ratio, symbol, order, held)
        if max_turnover is not None:
            ratio, symbol, order, held = max_turnover
            stats["max_sod_turnover"] = {
                **_order_stat(symbol, order),
                "sod_qty": held,
                "turnover_pct": ratio * 100,
            }
        if largest_new is not None:
            stats["largest_new_position"] = _order_stat(largest_new[0], largest_new[1])

    if results is not None:
        reasons = [rejection_reason_from_result(r) for r in results]
        rejected = [r for r in reasons if r]
        calc_warnings = sum(1 for r in rejected if is_exposure_calc_warning(r))
        stats["verdicts"] = {
            "submitted": len(results) - len(rejected),
            "rejected": len(rejected),
            "calc_warnings": calc_warnings,
            "true_rejections": len(rejected) - calc_warnings,
        }
    return stats


def _n(value: Decimal | int) -> str:
    """Whole-number formatting with thousands separators."""
    return f"{Decimal(value):,.0f}"


def _usd(value: Decimal) -> str:
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(Decimal(value)):,.0f}"


def _signed(value: Decimal, fmt=_n) -> str:
    return f"+{fmt(value)}" if value > 0 else fmt(value)


def _order_line(stat: dict[str, Any]) -> str:
    return f"{stat['symbol']} {stat['side']} {_n(stat['quantity'])} ({_usd(stat['notional'])})"


def format_ops_summary(stats: dict[str, Any], *, meta: dict[str, Any]) -> str:
    """The fenced Slack body (also printed to the submit log)."""
    lines = []
    flags = [k for k in ("no_route", "dry_run", "resend") if meta.get(k)]
    head = f"KOTL submit — {meta.get('trade_date')} {meta.get('env')}"
    if flags:
        head += " (" + ", ".join(f.replace("_", "-") for f in flags) + ")"
    if meta.get("strategy_id"):
        head += f"\nstrategy: {meta['strategy_id']}"
    lines.append(head)
    if meta.get("submit_id"):
        lines.append(f"submit_id: {meta['submit_id']}")

    lines.append(
        f"orders: {stats['orders']} ({stats['buys']} buys / {stats['sells']} sells)"
        + (
            f"   unresolved skipped: {stats['unresolved_skipped']}"
            if stats.get("unresolved_skipped")
            else ""
        )
    )
    verdicts = stats.get("verdicts")
    if verdicts:
        line = f"verdicts: {verdicts['submitted']} submitted, {verdicts['rejected']} rejected"
        if verdicts["rejected"]:
            line += (
                f" ({verdicts['calc_warnings']} exposure-calc warnings, "
                f"{verdicts['true_rejections']} true rejections)"
            )
        lines.append(line)

    shares, notional = stats["shares"], stats["notional"]
    lines.append(
        f"shares: buy {_n(shares['buy'])}  sell {_n(shares['sell'])}  "
        f"net {_signed(shares['net'])}"
    )
    lines.append(
        f"notional: gross {_usd(notional['gross'])}  buy {_usd(notional['buy'])}  "
        f"sell {_usd(notional['sell'])}  net {_signed(notional['net'], _usd)}"
    )

    if stats.get("max_order_shares"):
        lines.append(f"max order (shares): {_order_line(stats['max_order_shares'])}")
    if stats.get("max_order_notional"):
        lines.append(f"max order (notional): {_order_line(stats['max_order_notional'])}")
    if stats.get("max_sod_turnover"):
        t = stats["max_sod_turnover"]
        lines.append(
            f"max turnover vs SOD: {_order_line(t)} vs {_n(abs(t['sod_qty']))} held "
            f"— {t['turnover_pct']:.0f}%"
        )
    if stats.get("largest_new_position"):
        lines.append(f"largest new position: {_order_line(stats['largest_new_position'])}")
    if stats.get("top_orders_by_notional"):
        lines.append(
            "top by notional: "
            + "; ".join(_order_line(s) for s in stats["top_orders_by_notional"])
        )
    if meta.get("trade_file"):
        lines.append(f"trade file: {meta['trade_file']}")
    return "\n".join(lines)


def ops_summary_subject(stats: dict[str, Any], *, meta: dict[str, Any]) -> str:
    verdicts = stats.get("verdicts")
    subject = f"KOTL submit {meta.get('env')} {meta.get('trade_date')}"
    if meta.get("no_route"):
        subject += " (no-route)"
    if verdicts:
        subject += f" — {verdicts['submitted']}/{stats['orders']} submitted"
        if verdicts["calc_warnings"]:
            subject += f", {verdicts['calc_warnings']} calc-warnings"
        if verdicts["true_rejections"]:
            subject += f", {verdicts['true_rejections']} REJECTED"
    else:
        subject += f" — {stats['orders']} orders"
    return subject


def resolve_ops_webhook() -> str | None:
    """The #ops incoming webhook, resolved like the kelaidata gate task.

    ``KI_OPS_SLACK_WEBHOOK_URL`` wins (handy locally); otherwise the
    SecureString named by ``KI_OPS_SLACK_WEBHOOK_SSM_PARAM`` (default
    ``/kelaidata/slack/ops-webhook``) keeps the secret out of git and env
    blocks. Any lookup failure disables delivery, never the submit.
    """
    direct = (os.environ.get(ENV_SLACK_WEBHOOK) or "").strip()
    if direct:
        return direct
    param = (
        os.environ.get(ENV_SLACK_WEBHOOK_SSM) or ""
    ).strip() or DEFAULT_SLACK_WEBHOOK_SSM_PARAM
    try:
        import boto3

        value = boto3.client("ssm").get_parameter(Name=param, WithDecryption=True)
        return str(value["Parameter"]["Value"]).strip() or None
    except Exception as exc:  # noqa: BLE001 — never fail the submit
        print(f"slack: ops webhook lookup failed ({param}): {exc}")
        return None


def post_ops_summary(*, subject: str, body: str) -> bool:
    """Best-effort #ops delivery; returns whether the post succeeded."""
    webhook = resolve_ops_webhook()
    if not webhook:
        print("slack: no #ops webhook configured — submit summary not posted")
        return False
    try:
        send_slack(webhook_url=webhook, subject=subject, body=body)
    except Exception as exc:  # noqa: BLE001 — never fail the submit
        print(f"slack: submit summary delivery failed (ignored): {exc}")
        return False
    print("slack: submit summary posted to #ops")
    return True
