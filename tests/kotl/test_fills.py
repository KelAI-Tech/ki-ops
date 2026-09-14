"""ki-ops kotl fills: latest fills from the ledger (env-aware)."""

import json
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from ki_ops.cli import main
from ki_ops.kotl.models import WorkingOrder
from ki_ops.kotl.refresh import merge_order_snapshots
from ki_ops.kotl.store import KotlStore

TD_OLD = date(2026, 9, 9)
TD_NEW = date(2026, 9, 10)
TS = datetime(2026, 9, 10, 15, 30, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("KI_OPS_ENV", raising=False)
    monkeypatch.delenv("KOTL_FLEX_ENV", raising=False)


def _order(
    flex_order_id: str,
    symbol: str,
    *,
    trade_date: date = TD_NEW,
    side: str = "BUY",
    sent: int = 100,
    filled: int = 40,
    px: str = "190.5",
    seen: datetime = TS,
) -> WorkingOrder:
    return WorkingOrder.from_flex_snapshot(
        submit_id="s1",
        trade_date=trade_date,
        flex_order_id=flex_order_id,
        symbol=symbol,
        side=side,
        fund="KELAI",
        position_group="PG",
        unsigned_sent_qty=sent,
        unsigned_filled_qty=filled,
        avg_fill_px=px,
        last_seen_at=seen,
    )


@pytest.fixture()
def store(tmp_path) -> KotlStore:
    s = KotlStore(tmp_path / "kotl")
    s.upsert_working_orders(
        [
            _order("F1", "AAPL.US", filled=40),
            _order("F2", "MSFT.US", side="SELL", sent=50, filled=50, px="410.0"),
            _order("F3", "OLD.US", trade_date=TD_OLD, filled=10, seen=TS.replace(day=9)),
        ]
    )
    return s


def _fills(store, *extra, as_json=True):
    argv = ["kotl", "fills", "--store", "csv", "--data-dir", str(store.data_dir)]
    if as_json:
        argv.append("--json")
    argv.extend(extra)
    return main(argv)


# --- latest_trade_date -------------------------------------------------------


def test_csv_store_latest_trade_date(store, tmp_path):
    assert store.latest_trade_date() == TD_NEW
    assert KotlStore(tmp_path / "empty").latest_trade_date() is None


# --- CLI: ledger view --------------------------------------------------------


def test_fills_defaults_to_latest_trade_date(store, capsys):
    assert _fills(store) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["env"] == "canary"
    assert out["trade_date"] == TD_NEW.isoformat()
    assert out["count"] == 2
    assert {row["symbol"] for row in out["fills"]} == {"AAPL.US", "MSFT.US"}


def test_fills_explicit_trade_date(store, capsys):
    assert _fills(store, "--trade-date", TD_OLD.isoformat()) == 0
    out = json.loads(capsys.readouterr().out)
    assert [row["symbol"] for row in out["fills"]] == ["OLD.US"]
    assert out["fills"][0]["filled_qty"] == "10"


def test_fills_ticker_filter_normalizes_suffix(store, capsys):
    assert _fills(store, "--ticker", "aapl") == 0
    out = json.loads(capsys.readouterr().out)
    assert [row["symbol"] for row in out["fills"]] == ["AAPL.US"]
    assert out["fills"][0]["avg_fill_px"] == "190.5"


def test_fills_ticker_with_suffix_matches_too(store, capsys):
    assert _fills(store, "--ticker", "MSFT.US") == 0
    out = json.loads(capsys.readouterr().out)
    assert [row["symbol"] for row in out["fills"]] == ["MSFT.US"]
    assert out["fills"][0]["status"] == "done"


def test_fills_ticker_no_match_is_empty(store, capsys):
    assert _fills(store, "--ticker", "TSLA") == 0
    assert json.loads(capsys.readouterr().out)["fills"] == []


def test_fills_limit(store, capsys):
    assert _fills(store, "--limit", "1") == 0
    assert json.loads(capsys.readouterr().out)["count"] == 1


def test_fills_empty_ledger(tmp_path, capsys):
    empty = KotlStore(tmp_path / "empty")
    assert _fills(empty) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["trade_date"] is None
    assert out["fills"] == []


def test_fills_table_output(store, capsys):
    assert _fills(store, as_json=False) == 0
    out = capsys.readouterr().out
    assert "KOTL fills — 2026-09-10" in out
    assert "env=canary" in out
    assert "AAPL.US" in out and "MSFT.US" in out
    assert "total_abs_filled=90" in out


def test_fills_table_sorted_most_recent_first(store, capsys):
    # bump MSFT's last_seen_at so it sorts first
    msft = [o for o in store.load_working_orders(trade_date=TD_NEW) if o.symbol == "MSFT.US"][0]
    store.upsert_working_orders(
        [
            msft.with_flex_update(
                unsigned_filled_qty=50,
                last_seen_at=TS.replace(hour=16),
            )
        ]
    )
    assert _fills(store) == 0
    out = json.loads(capsys.readouterr().out)
    assert [row["symbol"] for row in out["fills"]] == ["MSFT.US", "AAPL.US"]


# --- CLI: --live (read-only merge) -------------------------------------------


class _FakeLiveSource:
    calls: list = []

    def __init__(self, config):
        self.config = config

    def fetch_orders(self, trade_date, *, stored):
        _FakeLiveSource.calls.append(trade_date)
        return [
            {
                "orderId": "F1",
                "filledQuantity": 100,
                "status": "FILLED",
                "weightedAvgPrice": 191.25,
            }
        ]


def test_fills_live_merges_but_does_not_write(store, capsys, monkeypatch):
    captured_envs = []

    def fake_load_flex_config(*, flex_env):
        captured_envs.append(flex_env)
        return {"env": flex_env}

    monkeypatch.setattr("ki_ops.kotl.flex_live.load_flex_config", fake_load_flex_config)
    monkeypatch.setattr("ki_ops.kotl.flex_live.LiveRefreshSource", _FakeLiveSource)

    assert _fills(store, "--live") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["live"] is True
    assert captured_envs == ["UAT"]  # canary preset

    by_symbol = {row["symbol"]: row for row in out["fills"]}
    assert by_symbol["AAPL.US"]["filled_qty"] == "100"
    assert by_symbol["AAPL.US"]["status"] == "done"
    assert by_symbol["AAPL.US"]["avg_fill_px"] == "191.25"
    # MSFT had no live snapshot: unchanged
    assert by_symbol["MSFT.US"]["filled_qty"] == "-50"

    # read-only: the ledger still holds the pre-merge fill state
    ledger = {o.symbol: o for o in store.load_working_orders(trade_date=TD_NEW)}
    assert ledger["AAPL.US"].filled_qty == Decimal("40")


def test_fills_live_flex_env_follows_prod_preset(store, capsys, monkeypatch):
    captured_envs = []

    def fake_load_flex_config(*, flex_env):
        captured_envs.append(flex_env)
        return {"env": flex_env}

    monkeypatch.setattr("ki_ops.kotl.flex_live.load_flex_config", fake_load_flex_config)
    monkeypatch.setattr("ki_ops.kotl.flex_live.LiveRefreshSource", _FakeLiveSource)
    monkeypatch.setenv("KI_OPS_ENV", "prod")

    assert _fills(store, "--live") == 0
    assert captured_envs == ["PROD"]
    json.loads(capsys.readouterr().out)  # still valid JSON


# --- merge helper -------------------------------------------------------------


def test_merge_order_snapshots_pure():
    stored = [_order("F1", "AAPL.US", filled=0, px=None), _order("F2", "MSFT.US")]
    updated = merge_order_snapshots(
        stored,
        [{"orderId": "F1", "filledQuantity": 60, "weightedAvgPrice": 190.0}],
        last_seen_at=TS,
    )
    assert len(updated) == 1
    assert updated[0].flex_order_id == "F1"
    assert updated[0].filled_qty == Decimal("60")
    assert updated[0].status.value == "partial"
    # inputs untouched
    assert stored[0].filled_qty == Decimal("0")
