"""LiveFlexAdapter / LiveRefreshSource / fetch_flex_positions against a fake SDK."""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from ki_ops.kotl import flex_live
from ki_ops.kotl.flex_live import (
    FlexConfig,
    FlexSdkMissingError,
    LiveFlexAdapter,
    LiveRefreshSource,
    fetch_flex_positions,
    is_plain_us_equity,
    load_flex_config,
)
from ki_ops.kotl.models import WorkingOrder
from ki_ops.kotl.store import KotlStore
from tests.kotl.fake_flex_sdk import (
    FakeFlexBackend,
    install_fake_sdk,
    make_create_result,
    make_order_info,
    make_position,
)

CONFIG = FlexConfig(endpoint="127.0.0.1:50051", token="tok", batch_user="KELAI-BATCH")


@pytest.fixture()
def backend(monkeypatch):
    be = FakeFlexBackend()
    install_fake_sdk(monkeypatch, be)
    return be


# ---------------------------------------------------------------------------
# config resolution
# ---------------------------------------------------------------------------


def test_flex_config_from_env(monkeypatch):
    monkeypatch.setenv("KOTL_FLEX_ENDPOINT", "172.31.88.47:50051")
    monkeypatch.setenv("KOTL_FLEX_TOKEN", "envtok")
    monkeypatch.setenv("KOTL_FLEX_BATCH_USER", "JCO")
    monkeypatch.setenv("KOTL_FLEX_SEND_TO_EMS", "false")
    cfg = load_flex_config(flex_env="UAT")
    assert cfg.endpoint == "172.31.88.47:50051"
    assert cfg.token == "envtok"
    assert cfg.batch_user == "JCO"
    assert cfg.send_to_ems is False
    assert cfg.metadata == [("authorization", "Bearer envtok")]


def test_flex_config_from_secret(monkeypatch):
    monkeypatch.delenv("KOTL_FLEX_ENDPOINT", raising=False)
    monkeypatch.delenv("KOTL_FLEX_TOKEN", raising=False)
    monkeypatch.delenv("KOTL_FLEX_BATCH_USER", raising=False)
    monkeypatch.delenv("KOTL_FLEX_SEND_TO_EMS", raising=False)
    monkeypatch.setattr(
        flex_live,
        "_load_flex_secret",
        lambda secret_id, region: {
            "token": "sekret",
            "uat_endpoint": "10.0.0.1:50051",
            "prod_endpoint": "10.0.0.2:50051",
            "metadata_key": "authorization",
            "scheme": "Bearer",
        },
    )
    cfg = load_flex_config(flex_env="UAT")
    assert cfg.endpoint == "10.0.0.1:50051"
    assert cfg.token == "sekret"
    assert cfg.send_to_ems is True

    prod = load_flex_config(flex_env="PROD")
    assert prod.endpoint == "10.0.0.2:50051"


def test_flex_config_env_endpoint_secret_token(monkeypatch):
    monkeypatch.setenv("KOTL_FLEX_ENDPOINT", "proxy:50051")
    monkeypatch.delenv("KOTL_FLEX_TOKEN", raising=False)
    monkeypatch.setattr(
        flex_live, "_load_flex_secret", lambda secret_id, region: {"token": "fromsecret"}
    )
    cfg = load_flex_config()
    assert cfg.endpoint == "proxy:50051"
    assert cfg.token == "fromsecret"


# ---------------------------------------------------------------------------
# SDK loading
# ---------------------------------------------------------------------------


def test_sdk_missing_raises_clear_error(monkeypatch):
    for mod in ("API", "API.Orders_pb2", "API.Orders_pb2_grpc", "API.DomainCommons_pb2"):
        monkeypatch.delitem(sys.modules, mod, raising=False)
    # If the developer shell exports KOTL_FLEX_SDK_PATH (the documented live
    # setup), an earlier test calling _load_sdk has already inserted the real
    # SDK onto sys.path permanently — strip it so the import genuinely fails.
    sdk_path = os.environ.get("KOTL_FLEX_SDK_PATH")
    if sdk_path:
        base = str(Path(sdk_path))
        stripped = [p for p in sys.path if p not in (base, str(Path(base) / "API"))]
        monkeypatch.setattr(sys, "path", stripped)
    monkeypatch.delenv("KOTL_FLEX_SDK_PATH", raising=False)
    with pytest.raises(FlexSdkMissingError, match="KOTL_FLEX_SDK_PATH"):
        flex_live._load_sdk(None)


# ---------------------------------------------------------------------------
# CreateOrders
# ---------------------------------------------------------------------------


def _payload(symbol: str, qty: float, side: str, *, origin_id: str | None = None) -> dict:
    return {
        "symbol": symbol,
        "quantity": qty,
        "side": side,
        **({"originId": origin_id} if origin_id else {}),
        "orderType": "MARKET",
        "fund": "KELAI",
        "positionGroup": "USATop2000_strategy_v1",
        "user": "SFA",
        "owner": "SFA",
        "trader": "SFA",
        "manualFill": False,
        "brokerAutomationType": "AUTOROUTE",
        "timeInForce": "GFD",
        "algo": "VWAP_AMRS",
        "broker": "KEL-GS-EQ-LT",
        "notes": "submit_id=abc",
    }


def test_create_orders_maps_fields_and_collects_stream(backend):
    # Flex echoes originId back as the result orderId, and the stream yields
    # results in completion order, not submission order (observed live in UAT
    # 2026-09-08) — the adapter must join on originId, not position.
    backend.create_results = [
        make_create_result("abc-3", success=False, description="bad symbol"),
        make_create_result("abc-1"),
        make_create_result("abc-2"),
    ]
    backend.create_chunk_size = 2  # results split across two streamed responses

    adapter = LiveFlexAdapter(CONFIG)
    orders = [
        _payload("AAPL.US", 18, "SELL", origin_id="abc-1"),
        _payload("MSFT.US", 30, "BUY", origin_id="abc-2"),
        _payload("ZZZ.US", 5, "BUY", origin_id="abc-3"),
    ]
    results = adapter.create_orders(orders)

    assert [r["orderId"] for r in results] == ["abc-1", "abc-2", "abc-3"]
    assert [r["success"] for r in results] == [True, True, False]
    assert [r["symbol"] for r in results] == ["AAPL.US", "MSFT.US", "ZZZ.US"]
    assert results[2]["description"] == "bad symbol"
    # CreateOrdersResponse.batchId is stamped on every result row (the ledger
    # persists it as working_orders.flex_batch_id).
    assert [r["batchId"] for r in results] == ["B1", "B1", "B1"]

    req = backend.last_create_request
    assert req.user == "KELAI-BATCH"  # configurable, not hardcoded "MGO"
    assert req.sendToEms is True
    assert req.complianceInputs.ruleSets == [0]  # PRE_TRADE appended

    first = req.orders[0]
    assert first.symbol == "AAPL.US"
    assert first.side == 1  # SELL enum
    assert first.orderType == 0  # MARKET enum
    assert first.timeInForce == 0  # GFD enum
    assert first.owner == "SFA"  # owner exists on the proto and is copied
    assert first.originId  # stamped for traceability
    assert first.notes == "submit_id=abc"
    assert first.brokerAutomation.predefinedType == 3  # AUTOROUTE
    assert not hasattr(first, "fund")  # no fund field on the Order proto

    assert backend.last_metadata == [("authorization", "Bearer tok")]
    assert backend.channels_opened[0].endpoint == "127.0.0.1:50051"
    assert backend.channels_opened[0].closed


def test_create_orders_send_to_ems_off(backend):
    backend.create_results = [make_create_result("abc-1")]
    adapter = LiveFlexAdapter(
        FlexConfig(endpoint="e:1", token="t", send_to_ems=False)
    )
    adapter.create_orders([_payload("AAPL.US", 1, "BUY", origin_id="abc-1")])
    assert backend.last_create_request.sendToEms is False


def test_create_orders_missing_result_raises(backend):
    backend.create_results = [make_create_result("abc-1")]
    adapter = LiveFlexAdapter(CONFIG)
    with pytest.raises(RuntimeError, match="no result for 1 submitted originId"):
        adapter.create_orders(
            [
                _payload("AAPL.US", 1, "BUY", origin_id="abc-1"),
                _payload("MSFT.US", 2, "BUY", origin_id="abc-2"),
            ]
        )


def test_create_orders_unknown_result_id_raises(backend):
    backend.create_results = [make_create_result("abc-1"), make_create_result("FLEX-99")]
    adapter = LiveFlexAdapter(CONFIG)
    with pytest.raises(RuntimeError, match="not among the submitted originIds"):
        adapter.create_orders(
            [
                _payload("AAPL.US", 1, "BUY", origin_id="abc-1"),
                _payload("MSFT.US", 2, "BUY", origin_id="abc-2"),
            ]
        )


def test_create_orders_keeps_last_result_per_origin(backend, capsys):
    # Observed live (UAT 2026-09-08): an order can produce interim results
    # before the terminal one — 2,082 results for 1,911 orders. The last
    # result per originId wins; extras are logged, order stays submission-order.
    backend.create_results = [
        make_create_result("abc-1", success=False, description="interim"),
        make_create_result("abc-2"),
        make_create_result("abc-1", success=True, description="booked"),
    ]
    adapter = LiveFlexAdapter(CONFIG)
    results = adapter.create_orders(
        [
            _payload("AAPL.US", 1, "BUY", origin_id="abc-1"),
            _payload("MSFT.US", 2, "BUY", origin_id="abc-2"),
        ]
    )
    assert [r["orderId"] for r in results] == ["abc-1", "abc-2"]
    assert results[0]["success"] is True  # terminal result, not the interim one
    out = capsys.readouterr().out
    assert "1 extra interim result(s) across 1 order(s)" in out


def test_create_orders_fund_split_children_join_to_parent(backend, capsys):
    # Observed live (UAT 2026-09-08): Flex splits an order across the position
    # group's fund allocations — results come back as <originId>-B/-C with no
    # result under the parent id (171 of 1,911 orders, exactly B+C each).
    backend.create_results = [
        make_create_result("abc-1-B"),
        make_create_result("abc-2"),
        make_create_result("abc-1-C"),
    ]
    adapter = LiveFlexAdapter(CONFIG)
    results = adapter.create_orders(
        [
            _payload("AAPL.US", 10, "BUY", origin_id="abc-1"),
            _payload("MSFT.US", 2, "BUY", origin_id="abc-2"),
        ]
    )
    assert [r["orderId"] for r in results] == ["abc-1", "abc-2"]
    assert results[0]["success"] is True
    assert results[0]["childOrderIds"] == ["abc-1-B", "abc-1-C"]
    assert "childOrderIds" not in results[1]
    assert "1 order(s) fund-split by Flex" in capsys.readouterr().out


def test_create_orders_fund_split_failing_child_fails_parent(backend):
    backend.create_results = [
        make_create_result("abc-1-B"),
        make_create_result("abc-1-C", success=False, description="no entitlement"),
    ]
    adapter = LiveFlexAdapter(CONFIG)
    (result,) = adapter.create_orders([_payload("AAPL.US", 10, "BUY", origin_id="abc-1")])
    assert result["success"] is False
    assert result["description"] == "no entitlement"


def test_aggregate_split_order_rows():
    from ki_ops.kotl.flex_live import aggregate_split_order_rows

    rows = [
        {"orderId": "p-1-B", "symbol": "AAPL.US", "quantity": 6.0, "filledQuantity": 6.0,
         "weightedAvgPrice": 100.0, "status": 2, "fund_acc_tgt": "KEL-B"},
        {"orderId": "p-1-C", "symbol": "AAPL.US", "quantity": 4.0, "filledQuantity": 2.0,
         "weightedAvgPrice": 101.0, "status": 1, "fund_acc_tgt": "KEL-C"},
        {"orderId": "p-2", "symbol": "MSFT.US", "quantity": 3.0, "filledQuantity": 0.0,
         "weightedAvgPrice": 0.0, "status": 1, "fund_acc_tgt": "KEL-B"},
    ]
    out = aggregate_split_order_rows(rows, ["p-1", "p-2"])
    by_id = {r["orderId"]: r for r in out}
    assert set(by_id) == {"p-1", "p-2"}
    merged = by_id["p-1"]
    assert merged["quantity"] == 10.0
    assert merged["filledQuantity"] == 8.0
    assert merged["weightedAvgPrice"] == pytest.approx((6 * 100 + 2 * 101) / 8)
    assert merged["status"] == 1  # least-filled child's status wins
    assert merged["fund_acc_tgt"] == ""  # mixed funds: no single parent fund
    assert merged["childOrderIds"] == ["p-1-B", "p-1-C"]
    assert by_id["p-2"]["quantity"] == 3.0  # unsplit rows pass through


def test_create_orders_empty_raises(backend):
    with pytest.raises(ValueError, match="empty"):
        LiveFlexAdapter(CONFIG).create_orders([])


# ---------------------------------------------------------------------------
# GetOrderInfo2 refresh source
# ---------------------------------------------------------------------------


def test_live_refresh_source_updates_ledger(backend, tmp_path):
    td = date(2026, 9, 4)
    store = KotlStore(tmp_path)
    wo = WorkingOrder.from_submit_line(
        submit_id="S1",
        flex_order_id="FLEX-1",
        trade_date=td,
        symbol="AAPL.US",
        side="SELL",
        fund="KELAI",
        position_group="USATop2000_strategy_v1",
        unsigned_sent_qty=18,
        submitted_at=datetime(2026, 9, 4, 14, 0, tzinfo=timezone.utc),
    )
    store.upsert_working_orders([wo])

    backend.order_infos = [
        make_order_info(
            "FLEX-1",
            "AAPL.US",
            side=1,
            quantity=18,
            filled_quantity=10,
            status=4,  # PARTIALLY_FILLED enum int
            weighted_avg_price=191.2,
            trade_date="09/04/2026",
        )
    ]

    from ki_ops.kotl.refresh import refresh_working_orders

    updated = refresh_working_orders(store, td, LiveRefreshSource(CONFIG))
    assert len(updated) == 1
    row = updated[0]
    assert row.filled_qty == Decimal("-10")
    assert row.leaves_qty == Decimal("-8")
    assert row.status.value == "partial"
    assert row.avg_fill_px == Decimal("191.2")

    # request used MM/DD/YYYY dates and ALL_ORDERS
    assert backend.last_query_request.fromDate == "09/04/2026"
    assert backend.last_query_request.toDate == "09/04/2026"
    assert backend.last_query_request.queryType == 4


# ---------------------------------------------------------------------------
# ReplayPositions
# ---------------------------------------------------------------------------


def test_is_plain_us_equity():
    assert is_plain_us_equity("NKE.US")
    assert is_plain_us_equity("BRK-B.US")
    assert not is_plain_us_equity("18880.KS")
    assert not is_plain_us_equity("AAPL 250117P00150000.US")  # option
    assert not is_plain_us_equity("AAPL")


def test_fetch_flex_positions_filters_and_signs(backend):
    backend.positions = [
        make_position("NKE.US", -100.0),
        make_position("AAPL.US", 250.0),
        make_position("18880.KS", 50.0),  # non-US listing → dropped
        make_position("AAPL 250117P00150000.US", -1.0),  # option → dropped
        make_position("TSLA.US", 10.0, fund="OTHERFUND"),  # wrong fund → dropped
        make_position("MSFT.US", 0.0),  # zero qty → dropped
    ]
    positions, raw = fetch_flex_positions(CONFIG)
    assert positions == {"NKE.US": Decimal("-100"), "AAPL.US": Decimal("250")}
    assert len(raw) == 6  # raw rows unfiltered for diagnostics
    assert backend.last_replay_request.sequenceId == 0


def test_fetch_flex_positions_group_markers(backend):
    from types import SimpleNamespace

    ours = make_position("NKE.US", -100.0)
    ours.attributes = [
        SimpleNamespace(key="positionGroup", value=SimpleNamespace(stringValue="USATop2000_strategy_v1"))
    ]
    theirs = make_position("IBM.US", 40.0)
    theirs.attributes = [
        SimpleNamespace(key="positionGroup", value=SimpleNamespace(stringValue="OTHER_group"))
    ]
    unmarked = make_position("AMD.US", 5.0)  # no group marker → fund filter governs
    backend.positions = [ours, theirs, unmarked]

    positions, _ = fetch_flex_positions(CONFIG)
    assert positions == {"NKE.US": Decimal("-100"), "AMD.US": Decimal("5")}
