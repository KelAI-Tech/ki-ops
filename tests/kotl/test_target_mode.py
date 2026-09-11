"""Target mode: residual math, sent sources, cross-check, claim, e2e re-runs."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from ki_ops.kotl.models import Submit, WorkingOrder
from ki_ops.kotl.store import KotlStore
from ki_ops.kotl.target_mode import (
    COVERED,
    FRESH,
    OVERSHOOT,
    PARTIAL,
    TargetModeViolation,
    apply_residuals,
    compute_residuals,
    crosscheck_ledger_vs_flex,
    sent_from_flex_rows,
    sent_from_ledger,
    write_residual_csv,
)
from ki_ops.models import Order, Side

TD = date(2026, 9, 8)
TS = datetime(2026, 9, 8, 13, 0, tzinfo=timezone.utc)

D = Decimal


def _order(symbol: str, side: Side, qty, px="10") -> Order:
    return Order(symbol, side, D(str(qty)), D(px), TS)


def _line(report, symbol):
    return next(l for l in report.lines if l.symbol == symbol)


# ---------------------------------------------------------------------------
# compute_residuals
# ---------------------------------------------------------------------------


def test_fresh_day_full_delta_goes_out():
    report = compute_residuals({"AAPL.US": D("50"), "MSFT.US": D("-30")}, {})
    assert report.fresh
    assert not report.covered
    assert {l.reason for l in report.lines} == {FRESH}
    assert _line(report, "AAPL.US").residual == D("50")
    assert _line(report, "MSFT.US").residual == D("-30")


def test_partial_prior_send_tops_up():
    report = compute_residuals({"AAPL.US": D("50")}, {"AAPL.US": D("20")})
    line = _line(report, "AAPL.US")
    assert line.reason == PARTIAL
    assert line.residual == D("30")
    # short side too
    report = compute_residuals({"MSFT.US": D("-30")}, {"MSFT.US": D("-10")})
    assert _line(report, "MSFT.US").residual == D("-20")


def test_complete_prior_send_is_covered():
    report = compute_residuals(
        {"AAPL.US": D("50"), "MSFT.US": D("-30")},
        {"AAPL.US": D("50"), "MSFT.US": D("-30")},
    )
    assert report.covered
    assert {l.reason for l in report.lines} == {COVERED}


def test_overshoot_clips_to_zero_never_reverses():
    # Regenerated lower target: 60 already sent, new delta only 50.
    report = compute_residuals({"AAPL.US": D("50")}, {"AAPL.US": D("60")})
    line = _line(report, "AAPL.US")
    assert line.reason == OVERSHOOT
    assert line.residual == D("0")
    assert report.covered  # nothing sendable
    assert "clipped to zero" in report.format_table()


def test_sent_symbol_absent_from_deltas_is_overshoot():
    # Recomputed book dropped TSLA entirely, but 40 shares already went out.
    report = compute_residuals({"AAPL.US": D("50")}, {"TSLA.US": D("40")})
    line = _line(report, "TSLA.US")
    assert line.reason == OVERSHOOT
    assert line.delta == D("0")
    assert line.residual == D("0")
    assert _line(report, "AAPL.US").residual == D("50")


def test_opposite_direction_prior_send_refuses():
    # sent −20 while today's delta is +50 → residual 70 > delta: inconsistent.
    with pytest.raises(TargetModeViolation, match="AAPL.US"):
        compute_residuals({"AAPL.US": D("50")}, {"AAPL.US": D("-20")})


def test_mixed_buys_sells_and_reports():
    report = compute_residuals(
        {"A.US": D("100"), "B.US": D("-50"), "C.US": D("10"), "D.US": D("7")},
        {"A.US": D("40"), "B.US": D("-50"), "C.US": D("15")},
    )
    assert _line(report, "A.US").residual == D("60")  # partial
    assert _line(report, "B.US").residual == D("0")  # covered
    assert _line(report, "C.US").reason == OVERSHOOT
    assert _line(report, "D.US").reason == FRESH
    table = report.format_table()
    assert "A.US" in table and "overshoot" in table
    rows = report.to_csv_rows()
    assert [r["symbol"] for r in rows] == ["A.US", "B.US", "C.US", "D.US"]


# ---------------------------------------------------------------------------
# apply_residuals
# ---------------------------------------------------------------------------


def _payloads_orders():
    payloads = [
        {"symbol": "AAPL.US", "side": "BUY", "quantity": 50.0},
        {"symbol": "MSFT.US", "side": "SELL", "quantity": 30.0},
    ]
    orders = [
        _order("AAPL", Side.BUY, 50),
        _order("MSFT", Side.SELL, 30),
    ]
    return payloads, orders


def test_apply_residuals_drops_covered_and_scales_partial():
    payloads, orders = _payloads_orders()
    report = compute_residuals(
        {"AAPL.US": D("50"), "MSFT.US": D("-30")},
        {"AAPL.US": D("20"), "MSFT.US": D("-30")},
    )
    kept_payloads, kept_orders = apply_residuals(payloads, orders, report)
    assert len(kept_payloads) == 1
    assert kept_payloads[0]["symbol"] == "AAPL.US"
    assert kept_payloads[0]["quantity"] == 30.0
    assert kept_orders[0].quantity == D("30")
    assert kept_orders[0].side is Side.BUY
    # original payload untouched (copied before mutation)
    assert payloads[0]["quantity"] == 50.0


def test_apply_residuals_fresh_day_passthrough():
    payloads, orders = _payloads_orders()
    report = compute_residuals({"AAPL.US": D("50"), "MSFT.US": D("-30")}, {})
    kept_payloads, kept_orders = apply_residuals(payloads, orders, report)
    assert kept_payloads == payloads
    assert kept_orders == orders


def test_apply_residuals_covered_book_empties():
    payloads, orders = _payloads_orders()
    report = compute_residuals(
        {"AAPL.US": D("50"), "MSFT.US": D("-30")},
        {"AAPL.US": D("50"), "MSFT.US": D("-30")},
    )
    kept_payloads, kept_orders = apply_residuals(payloads, orders, report)
    assert kept_payloads == [] and kept_orders == []


# ---------------------------------------------------------------------------
# sent_from_ledger
# ---------------------------------------------------------------------------


def _submit_row(submit_id, env, payloads, results):
    return Submit(
        submit_id=submit_id,
        submitted_at=TS,
        env=env,
        ok=all(r.get("success", True) for r in results),
        flex_order_ids=tuple(r["orderId"] for r in results),
        payload=tuple(payloads),
        flex_response={"results": results},
    )


def _wo(submit_id, order_id, symbol, side, sent, filled=0):
    return WorkingOrder.from_flex_snapshot(
        submit_id=submit_id,
        trade_date=TD,
        flex_order_id=order_id,
        symbol=symbol,
        side=side,
        fund="KELAI",
        position_group="PG",
        unsigned_sent_qty=sent,
        unsigned_filled_qty=filled,
        last_seen_at=TS,
    )


def test_sent_from_ledger_sums_accepted_orders_per_env():
    submits = [
        _submit_row(
            "s1",
            "UAT",
            [
                {"symbol": "AAPL.US", "side": "BUY", "quantity": 30.0},
                {"symbol": "MSFT.US", "side": "SELL", "quantity": 10.0},
            ],
            [{"orderId": "s1-1", "success": True}, {"orderId": "s1-2", "success": True}],
        ),
        # second attempt, one more AAPL lot
        _submit_row(
            "s2",
            "UAT",
            [{"symbol": "AAPL.US", "side": "BUY", "quantity": 20.0}],
            [{"orderId": "s2-1", "success": True}],
        ),
        # a PROD submit must not count toward UAT
        _submit_row(
            "s3",
            "PROD",
            [{"symbol": "AAPL.US", "side": "BUY", "quantity": 99.0}],
            [{"orderId": "s3-1", "success": True}],
        ),
    ]
    working = [
        _wo("s1", "s1-1", "AAPL.US", "BUY", 30),
        _wo("s1", "s1-2", "MSFT.US", "SELL", 10),
        _wo("s2", "s2-1", "AAPL.US", "BUY", 20),
        _wo("s3", "s3-1", "AAPL.US", "BUY", 99),
    ]
    sent = sent_from_ledger(submits, working, env="UAT")
    assert sent == {"AAPL.US": D("50"), "MSFT.US": D("-10")}


def test_sent_from_ledger_excludes_rejected_orders():
    submits = [
        _submit_row(
            "s1",
            "UAT",
            [
                {"symbol": "AAPL.US", "side": "BUY", "quantity": 30.0},
                {"symbol": "MSFT.US", "side": "BUY", "quantity": 10.0},
            ],
            [
                {"orderId": "s1-1", "success": True},
                {"orderId": "s1-2", "success": False},  # Flex rejected it
            ],
        ),
    ]
    working = [
        _wo("s1", "s1-1", "AAPL.US", "BUY", 30),
        _wo("s1", "s1-2", "MSFT.US", "BUY", 10),
    ]
    sent = sent_from_ledger(submits, working, env="UAT")
    assert sent == {"AAPL.US": D("30")}  # MSFT never made it to the market


def test_sent_from_ledger_subtract_fills_for_flex_sod():
    # 30 sent, 12 filled: a Flex SOD book already holds the 12 → count 18.
    submits = [
        _submit_row(
            "s1",
            "UAT",
            [{"symbol": "AAPL.US", "side": "BUY", "quantity": 30.0}],
            [{"orderId": "s1-1", "success": True}],
        ),
    ]
    working = [_wo("s1", "s1-1", "AAPL.US", "BUY", 30, filled=12)]
    assert sent_from_ledger(submits, working, env="UAT") == {"AAPL.US": D("30")}
    assert sent_from_ledger(submits, working, env="UAT", subtract_fills=True) == {
        "AAPL.US": D("18")
    }


def test_sent_from_ledger_ignores_other_trade_dates():
    # working_orders is pre-filtered by trade date; a submit with no working
    # order today contributes nothing.
    submits = [
        _submit_row(
            "yesterday",
            "UAT",
            [{"symbol": "AAPL.US", "side": "BUY", "quantity": 500.0}],
            [{"orderId": "y-1", "success": True}],
        ),
    ]
    assert sent_from_ledger(submits, [], env="UAT") == {}


# ---------------------------------------------------------------------------
# sent_from_flex_rows
# ---------------------------------------------------------------------------


def _flex_row(order_id, symbol, side, qty, *, filled=0.0, status=2, notes="submit_id=s1"):
    return {
        "orderId": order_id,
        "symbol": symbol,
        "side": side,
        "quantity": qty,
        "filledQuantity": filled,
        "status": status,
        "notes": notes,
    }


def test_sent_from_flex_rows_kotl_stamped_only():
    rows = [
        _flex_row("s1-1", "AAPL.US", 0, 30.0),
        _flex_row("s1-2", "MSFT.US", 1, 10.0),  # side 1 = SELL
        _flex_row("MANUAL-1", "AAPL.US", 0, 999.0, notes="desk order"),  # not KOTL
    ]
    sent = sent_from_flex_rows(rows)
    assert sent == {"AAPL.US": D("30"), "MSFT.US": D("-10")}


def test_sent_from_flex_rows_excludes_rejected_and_subtracts_fills():
    rows = [
        _flex_row("s1-1", "AAPL.US", 0, 30.0, filled=12.0),
        _flex_row("s1-2", "MSFT.US", 0, 10.0, status=6),  # 6 = REJECTED
    ]
    assert sent_from_flex_rows(rows) == {"AAPL.US": D("30")}
    assert sent_from_flex_rows(rows, subtract_fills=True) == {"AAPL.US": D("18")}


# ---------------------------------------------------------------------------
# crosscheck_ledger_vs_flex
# ---------------------------------------------------------------------------


def test_crosscheck_ok_when_ledger_matches_flex():
    ledger = [_wo("s1", "s1-1", "AAPL.US", "BUY", 30)]
    flex = [_flex_row("s1-1", "AAPL.US", 0, 30.0)]
    report = crosscheck_ledger_vs_flex(ledger, flex)
    assert report.ok
    assert report.checked == 1
    assert "OK" in report.format_table()


def test_crosscheck_flags_missing_and_qty_mismatch():
    ledger = [
        _wo("s1", "s1-1", "AAPL.US", "BUY", 30),
        _wo("s1", "s1-2", "MSFT.US", "SELL", 10),
    ]
    flex = [_flex_row("s1-1", "AAPL.US", 0, 25.0)]  # wrong qty; s1-2 missing
    report = crosscheck_ledger_vs_flex(ledger, flex)
    problems = {i.order_id: i.problem for i in report.issues}
    assert problems == {"s1-1": "qty-mismatch", "s1-2": "missing-from-flex"}


def test_crosscheck_flags_unknown_kotl_orders_in_flex():
    # The lost-ledger-write double-send scenario.
    flex = [
        _flex_row("lost-1", "AAPL.US", 0, 30.0),
        _flex_row("MANUAL-1", "TSLA.US", 0, 5.0, notes="desk order"),  # ignored
    ]
    report = crosscheck_ledger_vs_flex([], flex)
    assert [i.problem for i in report.issues] == ["unknown-in-flex"]
    assert report.issues[0].order_id == "lost-1"


# ---------------------------------------------------------------------------
# audit CSV + CSV-store claim
# ---------------------------------------------------------------------------


def test_write_residual_csv(tmp_path):
    report = compute_residuals({"AAPL.US": D("50")}, {"AAPL.US": D("20")})
    dest = tmp_path / "audit" / "target_mode_test.csv"
    written = write_residual_csv(report, dest)
    text = (tmp_path / "audit" / "target_mode_test.csv").read_text()
    assert written == str(dest)
    assert "symbol,delta,already_sent,residual,reason" in text
    assert "AAPL.US,50,20,30,partial" in text


def test_csv_store_claim_once_a_day(tmp_path):
    store = KotlStore(tmp_path)
    assert store.claim_submission(TD, "UAT", "sub-first") is None
    assert store.claim_submission(TD, "UAT", "sub-second") == "sub-first"
    # env/date are independent claims
    assert store.claim_submission(TD, "PROD", "sub-prod") is None
    assert store.claim_submission(date(2026, 9, 9), "UAT", "sub-next") is None


# ---------------------------------------------------------------------------
# end-to-end: forced re-run over the live adapter + fake SDK backend
# ---------------------------------------------------------------------------


@pytest.fixture()
def e2e(tmp_path, monkeypatch):
    pytest.importorskip("h5py")
    from ki_ops.kotl.flex_live import FlexConfig
    from tests.kotl.fake_flex_sdk import (
        FakeFlexBackend,
        install_fake_sdk,
        make_create_result,
        make_security,
    )
    from tests.kotl.test_kelaidata_source import make_ds2_h5

    shares = tmp_path / "Portfolio_20260806.csv"
    shares.write_text("AAPL,50,VWAP\nMSFT,-30,VWAP\n")
    backend = FakeFlexBackend()
    backend.security_master = [make_security("AAPL.US", 15), make_security("MSFT.US", 540)]
    install_fake_sdk(monkeypatch, backend)
    return {
        "store": KotlStore(tmp_path / "kotl"),
        "shares": shares,
        "h5": make_ds2_h5(tmp_path / "ds2.h5"),
        "cache": tmp_path / "cache",
        "backend": backend,
        "config": FlexConfig(endpoint="127.0.0.1:50051", token="tok"),
        "make_create_result": make_create_result,
    }


def _e2e_submit(e2e, **kwargs):
    from ki_ops.kotl.flex_live import LiveFlexAdapter
    from ki_ops.kotl.submit import submit_kelai_shares

    adapter = LiveFlexAdapter(e2e["config"])
    return submit_kelai_shares(
        e2e["store"],
        trade_date=date(2026, 8, 6),
        shares_file=e2e["shares"],
        ds2_h5=e2e["h5"],
        cache_dir=e2e["cache"],
        sod_source="flat",
        env="UAT",
        adapter=adapter,
        flex_config=e2e["config"],
        **kwargs,
    )


def _mirror_to_backend(e2e) -> None:
    """GetOrderInfo2 reflects everything CreateOrders accepted (healthy Flex)."""
    from tests.kotl.fake_flex_sdk import make_order_info

    e2e["backend"].order_infos = [
        make_order_info(
            o.flex_order_id,
            o.symbol,
            side=0 if o.side == "BUY" else 1,
            quantity=float(abs(o.sent_qty)),
            notes=f"submit_id={o.submit_id}",
        )
        for o in e2e["store"].load_working_orders(trade_date=date(2026, 8, 6))
    ]


def _echo_create_results(e2e) -> None:
    """CreateOrders echoes originIds back as result orderIds (live-verified)."""
    backend = e2e["backend"]
    original = backend.CreateOrders

    def create_orders(request, timeout=None, metadata=None):
        backend.create_results = [
            e2e["make_create_result"](order.originId) for order in request.orders
        ]
        yield from original(request, timeout=timeout, metadata=metadata)

    backend.CreateOrders = create_orders


def test_e2e_forced_rerun_sends_exactly_the_residual(e2e, capsys):
    _echo_create_results(e2e)
    first = _e2e_submit(e2e)
    assert first.ok
    assert len(first.payload) == 2
    _mirror_to_backend(e2e)

    # Re-run without --force: covered → clean no-op, nothing persisted.
    rerun = _e2e_submit(e2e)
    assert (rerun.flex_response or {}).get("target_covered") is True
    assert len(e2e["store"].load_submits()) == 1

    # Regenerate a bigger AAPL target; forced re-run sends only the top-up.
    e2e["shares"].write_text("AAPL,80,VWAP\nMSFT,-30,VWAP\n")
    forced = _e2e_submit(e2e, force=True)
    assert forced.ok
    assert len(forced.payload) == 1
    assert forced.payload[0]["symbol"] == "AAPL.US"
    assert forced.payload[0]["quantity"] == 30.0
    out = capsys.readouterr().out
    assert "capped to the residual" in out
    # Residual audit CSVs next to the trade file: one for the covered no-op
    # re-run, one for the forced top-up.
    trades_dir = e2e["store"].data_dir / "trades" / "20260806"
    audits = list(trades_dir.glob("target_mode_*.csv"))
    assert len(audits) == 2
    assert any("AAPL.US,80.0,50.0,30.0,partial" in p.read_text() for p in audits)


def test_e2e_sent_source_flex_recovers_lost_ledger(e2e):
    """Ledger lost the first submit: cross-check aborts, --sent-source flex
    recomputes already-sent from Flex and still refuses to double-trade."""
    from ki_ops.kotl.submit import SubmitRefusedError
    from tests.kotl.fake_flex_sdk import make_order_info

    # Flex holds today's KOTL orders, but the ledger is empty (lost write).
    e2e["backend"].order_infos = [
        make_order_info(
            "ghost-1", "AAPL.US", side=0, quantity=50.0, notes="submit_id=ghost"
        ),
        make_order_info(
            "ghost-2", "MSFT.US", side=1, quantity=30.0, notes="submit_id=ghost"
        ),
    ]

    with pytest.raises(SubmitRefusedError, match="cross-check failed"):
        _e2e_submit(e2e)

    # Recovery: trust Flex for already-sent. Target fully covered → no-op.
    recovered = _e2e_submit(e2e, sent_source="flex")
    assert (recovered.flex_response or {}).get("target_covered") is True
    assert e2e["store"].load_submits() == []


def test_e2e_dry_run_prints_residual_audit_without_grpc(e2e, capsys):
    """The canary nightly dry-run exercises the guard with no cross-check."""

    class ExplodingAdapter:
        def create_orders(self, order_list):
            raise AssertionError("dry run must not create orders")

    # Ledger says half of AAPL already went out.
    store = e2e["store"]
    store.append_submit(
        _submit_row(
            "s1",
            "UAT",
            [{"symbol": "AAPL.US", "side": "BUY", "quantity": 20.0}],
            [{"orderId": "s1-1", "success": True}],
        )
    )
    store.upsert_working_orders(
        [
            WorkingOrder.from_submit_line(
                submit_id="s1",
                flex_order_id="s1-1",
                trade_date=date(2026, 8, 6),
                symbol="AAPL.US",
                side="BUY",
                fund="KELAI",
                position_group="PG",
                unsigned_sent_qty=20,
                submitted_at=TS,
            )
        ]
    )

    from ki_ops.kotl.submit import submit_kelai_shares

    dry = submit_kelai_shares(
        store,
        trade_date=date(2026, 8, 6),
        shares_file=e2e["shares"],
        ds2_h5=e2e["h5"],
        cache_dir=e2e["cache"],
        sod_source="flat",
        env="UAT",
        adapter=ExplodingAdapter(),
        flex_config=e2e["config"],
        dry_run=True,
    )
    assert dry.flex_response == {"dry_run": True}
    assert len(dry.payload) == 2  # AAPL residual 30 + MSFT fresh 30
    aapl = next(p for p in dry.payload if p["symbol"] == "AAPL.US")
    assert aapl["quantity"] == 30.0
    out = capsys.readouterr().out
    assert "target mode — residual vs already-sent" in out
    assert e2e["backend"].last_query_request is None  # no GetOrderInfo2 on dry
