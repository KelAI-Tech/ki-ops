"""Submit path: Flex payloads → store (offline or live adapter)."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Protocol, Sequence

from ki_ops.kotl.fake_flex import FakeFlexAdapter
from ki_ops.kotl.flex_map import (
    FlexOrderDefaults,
    flex_orders_from_rebalance_csv,
    orders_to_flex_dicts,
)
from ki_ops.kotl.models import Submit, WorkingOrder, _utc
from ki_ops.kotl.store import KotlStore


class FlexSubmitAdapter(Protocol):
    def create_orders(self, order_list: list[dict]) -> list[dict]: ...


def submit_flex_orders(
    store: KotlStore,
    order_list: Sequence[dict],
    *,
    env: str = "FAKE",
    trade_date: date | None = None,
    adapter: FlexSubmitAdapter | None = None,
    submitted_at: datetime | None = None,
    submit_id: str | None = None,
) -> Submit:
    """Send *order_list* via *adapter*, persist submit + working orders."""
    if not order_list:
        raise ValueError("order_list is empty")

    adapter = adapter or FakeFlexAdapter()
    payloads = [dict(o) for o in order_list]

    if submit_id is None:
        bootstrap = Submit.new(env=env, ok=False, payload=(), submitted_at=submitted_at)
        submit_id = bootstrap.submit_id
        submitted_at = bootstrap.submitted_at
    else:
        submitted_at = _utc(submitted_at)

    # Re-stamp notes now that submit_id is known (if caller omitted it).
    for payload in payloads:
        notes = str(payload.get("notes") or "")
        if submit_id not in notes:
            extra = f"submit_id={submit_id}"
            payload["notes"] = f"{notes};{extra}".strip(";") if notes else extra

    results = adapter.create_orders(payloads)
    flex_ids = tuple(r["orderId"] for r in results)
    ok = all(r.get("success", True) for r in results)
    submit = Submit(
        submit_id=submit_id,
        submitted_at=submitted_at,
        env=env,
        ok=ok,
        flex_order_ids=flex_ids,
        payload=tuple(payloads),
        flex_response={"results": results},
    )

    td = trade_date or submit.submitted_at.date()
    working = [
        _working_order_from_submit(submit=submit, payload=payload, result=result, trade_date=td)
        for payload, result in zip(payloads, results)
    ]

    store.append_submit(submit)
    store.upsert_working_orders(working)
    return submit


def submit_rebalance_csv(
    store: KotlStore,
    sod_csv: str | Path,
    targets_csv: str | Path,
    *,
    env: str = "FAKE",
    trade_date: date | None = None,
    defaults: FlexOrderDefaults | None = None,
    symbol_suffix: str = ".US",
    adapter: FlexSubmitAdapter | None = None,
    submitted_at: datetime | None = None,
    flatten_missing_targets: bool = True,
) -> Submit:
    """``SOD + targets`` CSVs → fake/live submit → ``submits.csv`` + ``working_orders.csv``."""
    pending = Submit.new(env=env, ok=False, payload=(), submitted_at=submitted_at)
    payloads = flex_orders_from_rebalance_csv(
        sod_csv,
        targets_csv,
        defaults=defaults,
        symbol_suffix=symbol_suffix,
        submit_id=pending.submit_id,
        flatten_missing_targets=flatten_missing_targets,
    )
    return submit_flex_orders(
        store,
        payloads,
        env=env,
        trade_date=trade_date,
        adapter=adapter,
        submitted_at=pending.submitted_at,
        submit_id=pending.submit_id,
    )


def submit_kelai_shares(
    store: KotlStore,
    *,
    trade_date: date,
    shares_file: str | Path | None = None,
    ds2_h5: str | Path | None = None,
    sod_csv: str | Path | None = None,
    assume_flat_sod: bool = False,
    env: str = "FAKE",
    defaults: FlexOrderDefaults | None = None,
    symbol_suffix: str = ".US",
    adapter: FlexSubmitAdapter | None = None,
    submitted_at: datetime | None = None,
    cache_dir: str | Path | None = None,
) -> Submit:
    """kelaidata shares trade file (S3) + ds2 H5 prices → submit.

    Targets come from ``s3://kelaitrading/portfolio/shares/Portfolio_<YYYYMMDD>.csv``
    (signed whole-share target positions per ticker); prices and the ticker
    map come from ``ds2_data.h5`` on S3. Trades are ``target − SOD``; SOD must
    be given explicitly (*sod_csv*, ticker-keyed with ``quantity`` and
    ``market_price``) or waived with *assume_flat_sod* — there is no silent
    default book.
    """
    from ki_ops.intents import derive_trade_intents, load_sod_positions_csv
    from ki_ops.kotl.kelaidata_source import (
        DEFAULT_CACHE_DIR,
        DEFAULT_DS2_H5,
        default_shares_path,
        fetch,
        load_ds2_snapshot,
        load_shares_trade_file,
        targets_from_shares,
    )
    from ki_ops.models import Portfolio

    if sod_csv is None and not assume_flat_sod:
        raise ValueError(
            "SOD is required: pass sod_csv, or set assume_flat_sod=True to "
            "trade the full target book from a flat start"
        )

    cache = cache_dir or DEFAULT_CACHE_DIR
    shares_path = fetch(shares_file or default_shares_path(trade_date), cache_dir=cache)
    ds2_path = fetch(ds2_h5 or DEFAULT_DS2_H5, cache_dir=cache)

    shares = load_shares_trade_file(shares_path)
    snapshot = load_ds2_snapshot(ds2_path, trade_date=trade_date)
    targets = targets_from_shares(shares, snapshot)

    sod = load_sod_positions_csv(sod_csv) if sod_csv is not None else Portfolio()
    orders = derive_trade_intents(sod, targets, flatten_missing_targets=True)
    if not orders:
        raise ValueError(f"no trades: SOD already matches the {trade_date} target book")

    pending = Submit.new(env=env, ok=False, payload=(), submitted_at=submitted_at)
    payloads = orders_to_flex_dicts(
        orders,
        defaults=defaults,
        symbol_suffix=symbol_suffix,
        submit_id=pending.submit_id,
    )
    return submit_flex_orders(
        store,
        payloads,
        env=env,
        trade_date=trade_date,
        adapter=adapter,
        submitted_at=pending.submitted_at,
        submit_id=pending.submit_id,
    )


def _working_order_from_submit(
    *,
    submit: Submit,
    payload: dict,
    result: dict,
    trade_date: date,
) -> WorkingOrder:
    row = WorkingOrder.from_submit_line(
        submit_id=submit.submit_id,
        flex_order_id=result["orderId"],
        trade_date=trade_date,
        symbol=str(payload["symbol"]),
        side=str(payload["side"]),
        fund=str(payload.get("fund", "")),
        position_group=str(payload.get("positionGroup", "")),
        unsigned_sent_qty=payload["quantity"],
        submitted_at=submit.submitted_at,
    )
    return WorkingOrder(
        flex_order_id=row.flex_order_id,
        submit_id=row.submit_id,
        trade_date=row.trade_date,
        symbol=row.symbol,
        side=row.side,
        fund=row.fund,
        position_group=row.position_group,
        sent_qty=row.sent_qty,
        filled_qty=row.filled_qty,
        leaves_qty=row.leaves_qty,
        status=row.status,
        last_seen_at=row.last_seen_at,
        broker=payload.get("broker") or None,
        algo=payload.get("algo") or None,
        order_type=payload.get("orderType") or None,
    )
