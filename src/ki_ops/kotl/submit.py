"""Submit path: Flex payloads → store (offline or live adapter).

``submit_kelai_shares`` is the production entry point: kelaidata shares trade
file (targets) + ds2 prices + a **SOD source** → trade intents → Flex submit.
SOD sources:

- ``flex`` — live position book via ``ReplayPositions`` (``.US`` suffix
  stripped to bare tickers), with a reconciliation guard: the live book must
  match the latest **overnight book snapshot** (``kotl snapshot-book``,
  captured nightly after the close) — a trivial book-vs-book equality, no
  explanation heuristics, strict ``0/0`` by default. When no snapshot exists
  (bootstrap) the guard falls back to comparing against yesterday's target
  file;
- ``prior-target`` — yesterday's ``Portfolio_*.csv`` located next to today's
  (``ki_ops.gate.find_prior_file``);
- ``csv`` — explicit SOD CSV (legacy ``--sod``);
- ``flat`` — no book (legacy ``--assume-flat-sod``).

Safety rails on the live path:

- **market-hours gate** (:mod:`ki_ops.kotl.market_hours`): live submits are
  refused outside NYSE trading days 03:00 ET–close (exit 7) unless
  ``--allow-outside-market-hours`` is passed deliberately;
- **target mode** (:mod:`ki_ops.kotl.target_mode`): cumulative sends can never
  exceed the day's target book. Every live submit subtracts what was already
  sent today (ledger, cross-checked against live ``GetOrderInfo2``) and sends
  only the residual; a re-run with everything sent is a clean exit-0 no-op,
  and overshoot (regenerated lower target) clips to zero with a warning —
  never a corrective order. A ``kotl_submit_claims`` row is claimed atomically
  before ``CreateOrders`` so only one run per ``(trade_date, env)`` can send
  (``--force`` allows another attempt, still residual-capped);
- ``--dry-run`` (no gRPC, no ledger write, no claim);
- order-count / gross-notional caps checked before ``CreateOrders``;
- **pre-submit security resolution** via the Flex ``SecurityService``
  (:mod:`ki_ops.kotl.flex_symbols`): payload symbols are rewritten to the
  canonical master spelling and names absent from the master block the submit
  (``--unresolved block``, exit 6) or are skipped (``--unresolved skip``), with
  an ``unresolved_<submit_id>.csv`` report either way.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Protocol, Sequence

from ki_ops.kotl.fake_flex import FakeFlexAdapter
from ki_ops.kotl.flex_map import (
    FlexOrderDefaults,
    flex_orders_from_rebalance_csv,
    orders_to_flex_dicts,
)
from ki_ops.kotl.models import Submit, WorkingOrder, _utc
from ki_ops.kotl.qty import signed_qty
from ki_ops.kotl.store import KotlStore

SOD_SOURCES = ("flex", "prior-target", "csv", "flat")

# Config-driven safety defaults (env-overridable, strict recon / generous caps).
DEFAULT_RECON_MAX_SHARES = Decimal(os.environ.get("KOTL_RECON_MAX_SHARES", "0"))
DEFAULT_RECON_MAX_NAMES = int(os.environ.get("KOTL_RECON_MAX_NAMES", "0"))
DEFAULT_MAX_ORDERS = int(os.environ.get("KOTL_MAX_ORDERS", "5000"))
DEFAULT_MAX_GROSS_NOTIONAL = Decimal(os.environ.get("KOTL_MAX_GROSS_NOTIONAL", "100000000"))


class FlexSubmitAdapter(Protocol):
    def create_orders(self, order_list: list[dict]) -> list[dict]: ...


class ReconDivergenceError(RuntimeError):
    """Live Flex book vs recon-baseline divergence beyond thresholds (CLI exit 4)."""

    def __init__(self, message: str, report: "ReconReport") -> None:
        super().__init__(message)
        self.report = report


class SubmitRefusedError(RuntimeError):
    """Safety rail refusal — idempotency or caps (CLI exit 5)."""


class MarketClosedError(SubmitRefusedError):
    """Live submit attempted outside NYSE market hours (CLI exit 7).

    Raised before any network or S3 work when the wall clock is outside the
    submit window (NYSE trading days, 03:00 ET to the close — 16:00, or 13:00
    on early-close days; :mod:`ki_ops.kotl.market_hours`). Override with
    ``allow_outside_market_hours`` / ``--allow-outside-market-hours`` only for
    deliberate testing.
    """


class UnresolvedSecuritiesError(SubmitRefusedError):
    """Order symbols absent from the Flex security master (CLI exit 6).

    Raised before ``CreateOrders`` when pre-submit ``SecurityService`` lookup
    (FlexTrade's recommended workflow) cannot resolve every order symbol and
    the unresolved mode is ``block`` (the default). The unresolved list — the
    names to send FlexTrade so they seed the master — is on ``unresolved``
    and has already been written as a CSV report.
    """

    def __init__(self, message: str, unresolved: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.unresolved = list(unresolved)


@dataclass(frozen=True)
class ReconReport:
    """Per-symbol live-Flex-book vs recon-baseline diff (signed shares).

    The baseline is either the latest overnight **book snapshot** (canonical
    Flex symbols; the normal case) or, when no snapshot exists yet, the prior
    day's **target file** (bare ds2 tickers; bootstrap fallback) — the
    *baseline* label says which.
    """

    baseline: str
    diffs: tuple[tuple[str, Decimal, Decimal, Decimal], ...]  # symbol, live, baseline, diff
    total_abs_diff: Decimal
    names_diverged: int
    max_shares: Decimal
    max_names: int

    @property
    def breached(self) -> bool:
        return self.total_abs_diff > self.max_shares or self.names_diverged > self.max_names

    def format_table(self) -> str:
        header = f"SOD reconciliation — live Flex book vs {self.baseline}"
        if not self.diffs:
            return f"{header}\n  no divergence"
        cols = ("symbol", "flex_qty", "baseline_qty", "diff")
        table = [
            (sym, str(flex_qty), str(baseline_qty), str(diff))
            for sym, flex_qty, baseline_qty, diff in self.diffs
        ]
        widths = [len(c) for c in cols]
        for line in table:
            for i, cell in enumerate(line):
                widths[i] = max(widths[i], len(cell))
        fmt = lambda cells: "  ".join(c.ljust(widths[i]) for i, c in enumerate(cells))
        out = [header, fmt(cols), fmt(tuple("-" * w for w in widths))]
        out.extend(fmt(line) for line in table)
        out.append(
            f"total_abs_diff={self.total_abs_diff} names_diverged={self.names_diverged} "
            f"thresholds: max_shares={self.max_shares} max_names={self.max_names} "
            f"breached={self.breached}"
        )
        return "\n".join(out)


def reconcile_books(
    live: dict[str, Decimal],
    baseline_book: dict[str, Decimal],
    *,
    baseline: str,
    max_shares: Decimal = DEFAULT_RECON_MAX_SHARES,
    max_names: int = DEFAULT_RECON_MAX_NAMES,
) -> ReconReport:
    """Diff two books in the same symbol vocabulary — see :class:`ReconReport`."""
    symbols = sorted(set(live) | set(baseline_book))
    diffs = []
    total = Decimal("0")
    for sym in symbols:
        flex_qty = live.get(sym, Decimal("0"))
        baseline_qty = baseline_book.get(sym, Decimal("0"))
        diff = flex_qty - baseline_qty
        if diff != 0:
            diffs.append((sym, flex_qty, baseline_qty, diff))
            total += abs(diff)
    return ReconReport(
        baseline=baseline,
        diffs=tuple(diffs),
        total_abs_diff=total,
        names_diverged=len(diffs),
        max_shares=max_shares,
        max_names=max_names,
    )


def has_ok_submit(store, trade_date: date, env: str) -> Submit | None:
    """First ok submit for (trade_date, env) already in the ledger, if any."""
    submit_ids = {
        o.submit_id for o in store.load_working_orders(trade_date=trade_date)
    }
    for submit in store.load_submits():
        if submit.ok and submit.env == env and submit.submit_id in submit_ids:
            return submit
    return None


def submit_flex_orders(
    store: KotlStore,
    order_list: Sequence[dict],
    *,
    env: str = "FAKE",
    trade_date: date | None = None,
    adapter: FlexSubmitAdapter | None = None,
    submitted_at: datetime | None = None,
    submit_id: str | None = None,
    claim_submit_id: str | None = None,
) -> Submit:
    """Send *order_list* via *adapter*, persist submit + working orders.

    *claim_submit_id* is the ``kotl_submit_claims`` owner this attempt ran
    under (live claimed sends only) — persisted on the submit row so every
    attempt links back to the day's claim.
    """
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
    td = trade_date or submitted_at.date()

    # Re-stamp notes now that submit_id is known (if caller omitted it).
    for payload in payloads:
        notes = str(payload.get("notes") or "")
        if submit_id not in notes:
            extra = f"submit_id={submit_id}"
            payload["notes"] = f"{notes};{extra}".strip(";") if notes else extra

    results = adapter.create_orders(payloads)
    flex_ids = tuple(r["orderId"] for r in results)
    ok = all(r.get("success", True) for r in results)
    flex_response: dict = {"results": results}
    batch_ids = sorted({str(r.get("batchId") or "") for r in results} - {""})
    if batch_ids:
        flex_response["batchId"] = batch_ids[0] if len(batch_ids) == 1 else batch_ids
    submit = Submit(
        submit_id=submit_id,
        submitted_at=submitted_at,
        env=env,
        ok=ok,
        flex_order_ids=flex_ids,
        payload=tuple(payloads),
        flex_response=flex_response,
        trade_date=td,
        claim_submit_id=claim_submit_id,
    )

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


def _bare_ticker(symbol: str, *, suffix: str = ".US") -> str:
    sym = str(symbol).strip().upper()
    return sym[: -len(suffix)] if suffix and sym.endswith(suffix) else sym


def _sod_from_shares(
    shares_by_ticker: dict[str, Decimal],
    snapshot,
    target_tickers: set,
    *,
    label: str,
):
    """Ticker→qty book + ds2 prices → SOD :class:`Portfolio` (strict pricing)."""
    from ki_ops.models import Holding
    from ki_ops.portfolio import portfolio_from_holdings

    holdings = []
    unpriced = []
    for ticker, qty in shares_by_ticker.items():
        px = snapshot.price(ticker)
        if px is None:
            if ticker not in target_tickers:
                # SOD-only symbol would be flattened at an unknown price.
                unpriced.append(ticker)
                continue
            px = Decimal("0")  # target price wins in derive_trade_intents
        holdings.append(Holding(ticker, qty, px))
    if unpriced:
        shown = ", ".join(sorted(unpriced)[:20])
        raise ValueError(
            f"{len(unpriced)} {label} SOD tickers have no ds2 close and no "
            f"target row (cannot price their flatten orders): {shown}"
        )
    return portfolio_from_holdings(holdings, cash=Decimal("0"))


def _resolve_sod_source(
    sod_source: str | None,
    sod_csv: str | Path | None,
    assume_flat_sod: bool,
) -> str:
    if sod_source is not None:
        if sod_source not in SOD_SOURCES:
            raise ValueError(f"unknown sod_source {sod_source!r}; expected one of {SOD_SOURCES}")
        return sod_source
    # Backward compat: legacy --sod <csv> / --assume-flat-sod.
    if sod_csv is not None:
        return "csv"
    if assume_flat_sod:
        return "flat"
    raise ValueError(
        "SOD is required: pass sod_source ('flex', 'prior-target', 'csv', 'flat'), "
        "or the legacy sod_csv / assume_flat_sod arguments"
    )


def _sidecar_csv_dest(
    prefix: str,
    *,
    trade_date: date,
    submit_id: str,
    strategy_id: str | None,
    data_dir,
    trade_file_out: str | None,
    dry_run: bool,
) -> str:
    """``<prefix>_<submit_id>[_dryrun].csv`` alongside the trade file."""
    from ki_ops.kotl import trade_file as trade_file_mod

    base = trade_file_out or trade_file_mod.default_trade_file_dest(
        trade_date=trade_date,
        submit_id=submit_id,
        strategy_id=strategy_id,
        data_dir=data_dir,
        dry_run=dry_run,
    )
    name = f"{prefix}_{submit_id}{'_dryrun' if dry_run else ''}.csv"
    base = str(base)
    if "/" in base:
        return f"{base.rsplit('/', 1)[0]}/{name}"
    return name


def _load_book_sedols(
    sedol_source: str | None,
    infocode_by_ticker: dict[str, str],
    *,
    env: str,
    cache_dir,
) -> dict[str, str]:
    """Book ticker → SEDOL map for pre-submit Flex resolution.

    Sources (*sedol_source*, else ``KOTL_SEDOL_SOURCE``, default
    ``"snowflake"``):

    - ``"snowflake"`` — the daily kelai security master
      ``KELAI.LSEG[_CANARY].SECURITY_MASTER_DT``
      (:mod:`ki_ops.kotl.security_master`; schema picked by *env*);
    - a local path / ``s3://`` URL — CSV with ``infocode,sedol`` columns
      (offline override);
    - ``"none"`` / ``""`` — disable, resolution runs symbol-only.

    A load failure only warns — resolution then runs symbol-only, and
    genuinely unknown names still block the submit downstream.
    """
    source = sedol_source if sedol_source is not None else os.environ.get("KOTL_SEDOL_SOURCE")
    if source is None:
        source = "snowflake"
    source = str(source).strip()
    if source.lower() in ("", "none"):
        return {}
    try:
        if source.lower() == "snowflake":
            from ki_ops.kotl.security_master import fetch_sedols_by_ticker, secmaster_schema

            sedols = fetch_sedols_by_ticker(infocode_by_ticker, env=env)
            label = f"snowflake KELAI.{secmaster_schema(env)}.SECURITY_MASTER_DT"
        else:
            from ki_ops.kotl.flex_symbols import load_sedol_map, sedols_by_ticker

            sedols = sedols_by_ticker(
                load_sedol_map(source, cache_dir=cache_dir), infocode_by_ticker
            )
            label = source
    except Exception as exc:
        print(
            f"WARNING: SEDOL source {source} unavailable ({exc}) — "
            "flex symbol resolution will run symbol-only"
        )
        return {}
    print(f"sedol map: {len(sedols)} of {len(infocode_by_ticker)} book tickers ({label})")
    return sedols


def _resolve_payload_symbols(
    payloads: list[dict],
    orders: list,
    *,
    env: str,
    flex_config,
    symbol_suffix: str,
    data_dir,
    unresolved_mode: str,
    dry_run: bool,
    trade_date: date,
    submit_id: str,
    strategy_id: str | None,
    trade_file_out: str | None,
    sedols: dict[str, str] | None = None,
) -> tuple[list[dict], list]:
    """Pre-submit SecurityService resolution: rewrite payload symbols to canonical.

    Blocks (or skips, per *unresolved_mode*) names absent from the Flex master
    and writes the unresolved CSV report. On *dry_run* a resolution outage
    only warns and every unresolved consequence is reported, never raised.
    """
    from ki_ops.kotl.flex_symbols import (
        CACHE_FILENAME,
        build_unresolved_rows,
        format_resolution_summary,
        format_unresolved_table,
        resolve_flex_symbols,
        write_unresolved_csv,
    )

    tickers = [_bare_ticker(p["symbol"], suffix=symbol_suffix) for p in payloads]
    try:
        if flex_config is None:
            from ki_ops.kotl.flex_live import load_flex_config

            flex_config = load_flex_config(flex_env=env.upper())
        cache_path = Path(data_dir) / CACHE_FILENAME if data_dir is not None else None
        resolved, unresolved_names, details = resolve_flex_symbols(
            flex_config,
            tickers,
            sedols=sedols,
            suffix=symbol_suffix,
            cache_path=cache_path,
        )
    except Exception as exc:
        if dry_run:
            print(
                f"DRY RUN: flex symbol resolution unavailable ({exc}) — "
                "payload symbols left as-is"
            )
            return payloads, orders
        raise

    print(format_resolution_summary(resolved, unresolved_names, details))

    # Rewrite in place: canonical master symbol out, original kept for audit.
    for payload in payloads:
        bare = _bare_ticker(payload["symbol"], suffix=symbol_suffix)
        canonical = resolved.get(bare)
        if canonical and canonical != payload["symbol"]:
            payload["sourceSymbol"] = payload["symbol"]
            payload["symbol"] = canonical

    if not unresolved_names:
        return payloads, orders

    rows = build_unresolved_rows(unresolved_names, details, payloads, suffix=symbol_suffix)
    print(format_unresolved_table(rows))
    dest = _sidecar_csv_dest(
        "unresolved",
        trade_date=trade_date,
        submit_id=submit_id,
        strategy_id=strategy_id,
        data_dir=data_dir,
        trade_file_out=trade_file_out,
        dry_run=dry_run,
    )
    written = write_unresolved_csv(rows, dest)
    print(f"unresolved securities file: {written}")

    if unresolved_mode == "block":
        message = (
            f"{len(unresolved_names)} order symbol(s) not in the Flex security "
            f"master (see {written}) — send the list to FlexTrade, or pass "
            "--unresolved skip to submit resolved names only"
        )
        if dry_run:
            print(f"DRY RUN: UNRESOLVED SECURITIES — a live submit would block (exit 6): {message}")
            return payloads, orders
        raise UnresolvedSecuritiesError(
            f"UNRESOLVED SECURITIES: {message}", unresolved_names
        )

    # skip mode: submit resolved names only.
    unresolved_set = set(unresolved_names)
    kept = [
        (payload, order)
        for payload, order in zip(payloads, orders)
        if _bare_ticker(str(payload.get("sourceSymbol") or payload["symbol"]), suffix=symbol_suffix)
        not in unresolved_set
    ]
    if not kept:
        raise UnresolvedSecuritiesError(
            "UNRESOLVED SECURITIES: no order symbol resolved against the Flex "
            f"security master (see {written})",
            unresolved_names,
        )
    print(
        f"--unresolved skip: submitting {len(kept)} resolved order(s), "
        f"skipping {len(unresolved_names)} unresolved"
    )
    return [p for p, _ in kept], [o for _, o in kept]


def submit_kelai_shares(
    store,
    *,
    trade_date: date,
    shares_file: str | Path | None = None,
    ds2_h5: str | Path | None = None,
    sod_csv: str | Path | None = None,
    assume_flat_sod: bool = False,
    sod_source: str | None = None,
    strategy_id: str | None = None,
    env: str = "FAKE",
    defaults: FlexOrderDefaults | None = None,
    symbol_suffix: str = ".US",
    adapter: FlexSubmitAdapter | None = None,
    submitted_at: datetime | None = None,
    cache_dir: str | Path | None = None,
    flex_positions: dict[str, Decimal] | None = None,
    flex_config=None,
    recon_max_shares: Decimal | None = None,
    recon_max_names: int | None = None,
    dry_run: bool = False,
    force: bool = False,
    max_orders: int | None = None,
    max_gross_notional: Decimal | None = None,
    trade_file_out: str | None = None,
    write_trade_file: bool = True,
    unresolved: str = "block",
    sedol_source: str | None = None,
    sent_source: str = "ledger",
    allow_outside_market_hours: bool = False,
) -> Submit:
    """kelaidata shares trade file (S3) + ds2 H5 prices + SOD source → submit.

    Targets come from ``s3://kelaitrading/portfolio/shares/[<strategy_id>/]``
    ``Portfolio_<YYYYMMDD>.csv`` (signed whole-share target positions per
    ticker); prices and the ticker map come from ``ds2_data.h5`` on S3. Trades
    are ``target − SOD``; the SOD book comes from *sod_source* (see module
    docstring) — there is no silent default book.

    **SOD recon** (``flex`` SOD only): before anything is sent, the live book
    is diffed against the latest overnight **book snapshot** captured by the
    nightly ``kotl snapshot-book`` job (:mod:`ki_ops.kotl.book_snapshot`) —
    both books in canonical Flex symbols, so it is a trivial equality check.
    Overnight nothing should move, so the strict default thresholds (``0/0``,
    *recon_max_shares* / *recon_max_names*) hold on a normal day regardless of
    how yesterday's orders filled — partial fills, rejections and multi-step
    sends are already inside the snapshot. Divergence means overnight drift
    (manual trades, corporate actions, a Flex-side book change) and blocks
    with :class:`ReconDivergenceError` (exit 4) until thresholds are raised
    deliberately. Bootstrap fallback when no snapshot exists yet: the diff
    runs against yesterday's target file (bare tickers) instead — on that
    basis unexecuted orders do show as divergence and need a deliberate
    threshold override.

    **Pre-submit security resolution** (FlexTrade's recommended workflow): for
    live envs (UAT/PROD) every payload symbol is checked through the Flex
    ``SecurityService`` first (:mod:`ki_ops.kotl.flex_symbols`) and rewritten
    to the canonical master symbol (``BFB.US → BF/B.US``). Lookup tries the
    **SEDOL first** (FlexTrade's preferred identifier): *sedol_source*
    (default ``"snowflake"``) maps each book infocode to its SEDOL via the
    daily kelai security master ``KELAI.LSEG[_CANARY].SECURITY_MASTER_DT``
    (:mod:`ki_ops.kotl.security_master`); pass a CSV path/URL for an offline
    map or ``"none"`` for symbol-only resolution. Ledger *pricing*
    keys stay in the ds2 (undotted) vocabulary — only the outgoing payload
    symbol changes; the original spelling is kept on the payload as
    ``sourceSymbol``. Working orders therefore store the **canonical Flex
    symbol**, which is what ``GetOrderInfo2`` echoes back on refresh (refresh
    joins on ``originId``/``orderId``, so this keeps the ledger coherent).
    Names absent from the master **block the submit** (*unresolved*
    ``"block"``, :class:`UnresolvedSecuritiesError`, CLI exit 6) unless
    *unresolved* is ``"skip"`` (submit resolved names only); either way the
    unresolved list is printed and written as ``unresolved_<submit_id>.csv``
    next to the trade file — that CSV is the list to send FlexTrade so they
    add the securities. On ``dry_run`` resolution is still attempted and
    reported, but a resolution outage (no network to Flex) only warns.

    **Target mode** (live envs; :mod:`ki_ops.kotl.target_mode`): the payloads
    are capped to the **residual** — intended delta minus what was already
    sent today for ``(trade_date, env)``. Re-running is therefore always safe:
    with everything sent the run is a clean no-op (empty-payload ``Submit``
    with ``flex_response["target_covered"]``, CLI exit 0); a prior partial
    send tops up; overshoot (a regenerated *lower* target) clips to zero with
    a loud warning and **never** generates a corrective order. Already-sent
    comes from the ledger (*sent_source* ``"ledger"``, default) and is
    cross-checked **both ways** against live ``GetOrderInfo2`` before any
    send — a mismatch refuses the submit (exit 5). ``sent_source="flex"`` is
    the lost/corrupted-ledger recovery: already-sent is recomputed from Flex
    (KOTL-stamped orders only) and the cross-check becomes informational; the
    residual cap itself can never be bypassed. Before ``CreateOrders`` the run
    atomically claims ``(trade_date, env)`` in the store — a second run is
    refused (exit 5) unless *force*, and *force* still only sends the
    residual.

    On ``dry_run`` the returned :class:`Submit` is **not** persisted and has no
    flex order ids; everything else (recon table, residual audit, trade file,
    caps report) is still produced — but no claim is taken and the live
    cross-check is skipped (no gRPC).
    """
    from ki_ops.intents import derive_trade_intents, load_sod_positions_csv
    from ki_ops.kotl.kelaidata_source import (
        DEFAULT_CACHE_DIR,
        DEFAULT_DS2_H5,
        default_shares_path,
        fetch,
        load_ds2_snapshot,
        load_shares_trade_file,
        normalize_tickers_to_ds2,
        targets_from_shares,
    )
    from ki_ops.kotl import trade_file as trade_file_mod
    from ki_ops.models import Portfolio

    from ki_ops.kotl import target_mode

    if unresolved not in ("block", "skip"):
        raise ValueError(f"unresolved must be 'block' or 'skip', got {unresolved!r}")
    if sent_source not in target_mode.SENT_SOURCES:
        raise ValueError(
            f"sent_source must be one of {target_mode.SENT_SOURCES}, got {sent_source!r}"
        )
    live = env.upper() in ("UAT", "PROD")
    if sent_source == "flex" and not live:
        raise ValueError("sent_source='flex' requires a live env (UAT/PROD)")

    # --- market-hours gate (live envs; fail fast, before any S3/gRPC work) ---
    if live:
        from ki_ops.kotl.market_hours import market_hours_verdict

        market_open, reason = market_hours_verdict()
        if market_open:
            print(f"market hours OK: {reason}")
        elif allow_outside_market_hours:
            print(
                f"MARKET CLOSED — proceeding anyway "
                f"(--allow-outside-market-hours): {reason}"
            )
        elif dry_run:
            print(f"DRY RUN: MARKET CLOSED — a live submit would block (exit 7): {reason}")
        else:
            raise MarketClosedError(
                f"MARKET CLOSED: {reason} — live orders only go out on NYSE "
                "trading days between 03:00 ET and the close; pass "
                "--allow-outside-market-hours to override deliberately"
            )

    source = _resolve_sod_source(sod_source, sod_csv, assume_flat_sod)
    recon_max_shares = (
        DEFAULT_RECON_MAX_SHARES if recon_max_shares is None else Decimal(recon_max_shares)
    )
    recon_max_names = DEFAULT_RECON_MAX_NAMES if recon_max_names is None else int(recon_max_names)
    max_orders = DEFAULT_MAX_ORDERS if max_orders is None else int(max_orders)
    max_gross_notional = (
        DEFAULT_MAX_GROSS_NOTIONAL if max_gross_notional is None else Decimal(max_gross_notional)
    )

    cache = cache_dir or DEFAULT_CACHE_DIR
    shares_url = str(shares_file or default_shares_path(trade_date, strategy_id=strategy_id))
    shares_path = fetch(shares_url, cache_dir=cache)
    ds2_path = fetch(ds2_h5 or DEFAULT_DS2_H5, cache_dir=cache)

    snapshot = load_ds2_snapshot(ds2_path, trade_date=trade_date)
    shares = normalize_tickers_to_ds2(
        load_shares_trade_file(shares_path), snapshot, label="targets"
    )
    targets = targets_from_shares(shares, snapshot)
    target_tickers = set(shares)

    def _prior_target_book() -> "tuple[dict[str, Decimal], str] | None":
        from ki_ops.gate import find_prior_file

        prior = find_prior_file(shares_url, trade_date)
        if prior is None:
            return None
        prior_path = fetch(prior, cache_dir=cache)
        book = normalize_tickers_to_ds2(
            load_shares_trade_file(prior_path), snapshot, label="prior-target SOD"
        )
        return book, prior

    # --- SOD book ----------------------------------------------------------
    if source == "csv":
        if sod_csv is None:
            raise ValueError("sod_source='csv' requires sod_csv")
        sod = load_sod_positions_csv(sod_csv)
    elif source == "flat":
        sod = Portfolio()
    elif source == "flex":
        if flex_positions is None:
            from ki_ops.kotl.flex_live import fetch_flex_positions, load_flex_config

            flex_config = flex_config or load_flex_config(
                flex_env=env if env.upper() in ("UAT", "PROD") else "UAT"
            )
            flex_defaults = defaults or FlexOrderDefaults()
            # account/fund scoping use the live-verified booking defaults
            # (KELAI / KEL-LOMB, env-overridable) — NOT the payload fund key.
            flex_positions, _ = fetch_flex_positions(
                flex_config,
                position_group=flex_defaults.position_group,
                symbol_suffix=symbol_suffix,
            )
        bare_book = normalize_tickers_to_ds2(
            {
                _bare_ticker(sym, suffix=symbol_suffix): qty
                for sym, qty in flex_positions.items()
            },
            snapshot,
            label="flex SOD",
        )
        sod = _sod_from_shares(bare_book, snapshot, target_tickers, label="flex")

        # Reconciliation guard: the live book must match the latest overnight
        # book snapshot (``kotl snapshot-book``, captured nightly after the
        # close). Book-vs-book in canonical Flex symbols — overnight nothing
        # should move, so the strict 0/0 default thresholds are the right
        # ones and need no explanation heuristics. Bootstrap fallback (no
        # snapshot yet): compare against yesterday's target file in bare
        # tickers — a routine partial-fill day then shows as divergence and
        # needs a deliberate threshold override.
        def _recon_guard(recon: ReconReport) -> None:
            print(recon.format_table())
            if recon.breached and not dry_run:
                raise ReconDivergenceError(
                    f"live Flex book diverges from {recon.baseline}: "
                    f"total_abs_diff={recon.total_abs_diff} shares over "
                    f"{recon.names_diverged} names (max_shares={recon_max_shares}, "
                    f"max_names={recon_max_names}) — raise --recon-max-shares/"
                    "--recon-max-names deliberately, or fix the book",
                    recon,
                )
            if recon.breached:
                print("DRY RUN: recon thresholds breached — a live submit would abort (exit 4)")

        snap = store.load_latest_book_snapshot(env, before=trade_date)
        if snap is not None:
            snap_as_of, snap_book = snap
            age_days = (trade_date - snap_as_of).days
            if age_days > 4:
                print(
                    f"WARNING: latest {env} book snapshot is {age_days} days old "
                    f"(as of {snap_as_of.isoformat()}) — is the nightly "
                    "snapshot-book job running?"
                )
            _recon_guard(
                reconcile_books(
                    {str(sym).upper(): qty for sym, qty in flex_positions.items()},
                    snap_book,
                    baseline=f"book snapshot as of {snap_as_of.isoformat()}",
                    max_shares=recon_max_shares,
                    max_names=recon_max_names,
                )
            )
        else:
            prior = _prior_target_book()
            if prior is None:
                print(
                    f"WARNING: no {env} book snapshot and no prior Portfolio_*.csv "
                    f"before {trade_date.isoformat()} next to {shares_url} — "
                    "skipping SOD reconciliation"
                )
            else:
                prior_book, prior_url = prior
                print(
                    f"WARNING: no {env} book snapshot before "
                    f"{trade_date.isoformat()} (run kotl snapshot-book nightly) — "
                    "recon falls back to the prior target file"
                )
                _recon_guard(
                    reconcile_books(
                        bare_book,
                        prior_book,
                        baseline=f"prior target ({prior_url})",
                        max_shares=recon_max_shares,
                        max_names=recon_max_names,
                    )
                )
    elif source == "prior-target":
        prior = _prior_target_book()
        if prior is None:
            raise ValueError(
                f"sod_source='prior-target': no prior Portfolio_*.csv before "
                f"{trade_date.isoformat()} next to {shares_url}"
            )
        prior_book, prior_url = prior
        print(f"SOD from prior target file: {prior_url}")
        sod = _sod_from_shares(prior_book, snapshot, target_tickers, label="prior-target")
    else:  # pragma: no cover — _resolve_sod_source guards
        raise ValueError(f"unhandled sod_source {source!r}")

    # --- intents + payloads -------------------------------------------------
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

    # --- pre-submit security resolution (live envs; see docstring) -----------
    if env.upper() in ("UAT", "PROD"):
        payload_tickers = {_bare_ticker(p["symbol"], suffix=symbol_suffix) for p in payloads}
        sedols = _load_book_sedols(
            sedol_source,
            {
                ticker: infocode
                for ticker, infocode in snapshot.infocode_by_ticker.items()
                if ticker in payload_tickers
            },
            env=env,
            cache_dir=cache,
        )
        payloads, orders = _resolve_payload_symbols(
            payloads,
            orders,
            env=env,
            flex_config=flex_config,
            symbol_suffix=symbol_suffix,
            data_dir=getattr(store, "data_dir", None),
            unresolved_mode=unresolved,
            dry_run=dry_run,
            trade_date=trade_date,
            submit_id=pending.submit_id,
            strategy_id=strategy_id,
            trade_file_out=trade_file_out,
            sedols=sedols,
        )

    # --- target mode: cross-check + residual guard (see docstring) -----------
    if live:
        # A Flex-sourced SOD book already contains today's fills, so the filled
        # part of every sent order is inside target − SOD — subtract it.
        subtract_fills = source == "flex"
        all_submits = store.load_submits()
        env_submit_ids = {s.submit_id for s in all_submits if s.env == env}
        today_working = store.load_working_orders(trade_date=trade_date)
        env_working = [w for w in today_working if w.submit_id in env_submit_ids]

        flex_rows = None
        if not dry_run or sent_source == "flex":
            from ki_ops.kotl.flex_live import (
                aggregate_split_order_rows,
                fetch_order_rows,
                load_flex_config,
            )

            if flex_config is None:
                flex_config = load_flex_config(flex_env=env.upper())
            raw_rows = fetch_order_rows(flex_config, trade_date)
            flex_rows = aggregate_split_order_rows(
                raw_rows, [w.flex_order_id for w in env_working]
            )
            cross = target_mode.crosscheck_ledger_vs_flex(env_working, flex_rows)
            if env_working or not cross.ok:
                print(cross.format_table())
            if not cross.ok and sent_source == "ledger":
                message = (
                    f"ledger vs Flex cross-check failed for {trade_date.isoformat()} "
                    f"{env}: {len(cross.issues)} issue(s) — the ledger cannot be "
                    "trusted as the already-sent source. Investigate; if the ledger "
                    "lost a write, re-run with --sent-source flex (the target cap "
                    "still applies)"
                )
                if dry_run:
                    print(
                        "DRY RUN: cross-check failed — a live submit would refuse "
                        f"(exit 5): {message}"
                    )
                else:
                    raise SubmitRefusedError(message)

        if sent_source == "flex":
            sent = target_mode.sent_from_flex_rows(flex_rows, subtract_fills=subtract_fills)
            print(
                f"already-sent source: flex GetOrderInfo2 "
                f"({len(sent)} symbol(s) with KOTL-stamped sends today)"
            )
        else:
            sent = target_mode.sent_from_ledger(
                all_submits, today_working, env=env, subtract_fills=subtract_fills
            )

        deltas = {
            str(p["symbol"]).upper(): signed_qty(p["side"], p["quantity"]) for p in payloads
        }
        try:
            residual_report = target_mode.compute_residuals(deltas, sent)
        except target_mode.TargetModeViolation as exc:
            raise SubmitRefusedError(f"TARGET MODE: {exc}") from exc
        print(residual_report.format_table())
        if not residual_report.fresh:
            audit_dest = _sidecar_csv_dest(
                "target_mode",
                trade_date=trade_date,
                submit_id=pending.submit_id,
                strategy_id=strategy_id,
                data_dir=getattr(store, "data_dir", None),
                trade_file_out=trade_file_out,
                dry_run=dry_run,
            )
            written = target_mode.write_residual_csv(residual_report, audit_dest)
            print(f"residual audit file: {written}")
        payloads, orders = target_mode.apply_residuals(payloads, orders, residual_report)
        if not payloads:
            print(
                f"TARGET COVERED: every {trade_date.isoformat()} {env} delta was "
                "already sent — nothing to submit (clean no-op)"
            )
            return Submit(
                submit_id=pending.submit_id,
                submitted_at=pending.submitted_at,
                env=env,
                ok=True,
                flex_order_ids=(),
                payload=(),
                flex_response={"target_covered": True, "dry_run": dry_run},
                trade_date=trade_date,
            )

    # --- safety rails --------------------------------------------------------
    gross_notional = sum((abs(o.quantity) * o.limit_price for o in orders), Decimal("0"))
    cap_breaches = []
    if len(payloads) > max_orders:
        cap_breaches.append(f"order count {len(payloads)} > max_orders {max_orders}")
    if gross_notional > max_gross_notional:
        cap_breaches.append(
            f"gross notional {gross_notional} > max_gross_notional {max_gross_notional}"
        )
    if cap_breaches:
        message = "; ".join(cap_breaches)
        if dry_run:
            print(f"DRY RUN: caps breached — a live submit would refuse (exit 5): {message}")
        else:
            raise SubmitRefusedError(f"refusing submit before CreateOrders: {message}")

    # --- once-a-day atomic claim (before CreateOrders; races lose here) -------
    # Every persisted live submit links back to the day's claim row via
    # claim_submit_id: the winner points at itself, forced follow-ups at the
    # winner.
    claim_submit_id = None
    if not dry_run and live:
        existing_claim = store.claim_submission(trade_date, env, pending.submit_id)
        claim_submit_id = pending.submit_id if existing_claim is None else existing_claim
        if existing_claim is not None:
            if not force:
                raise SubmitRefusedError(
                    f"trade_date={trade_date.isoformat()} env={env} is already "
                    f"claimed (submit_id={existing_claim}) and a residual remains — "
                    "pass --force to send it (target mode caps the send; a re-run "
                    "with everything sent is a clean no-op without --force)"
                )
            print(
                f"--force: proceeding past the existing {trade_date.isoformat()} "
                f"{env} claim ({existing_claim}) — this send is capped to the residual"
            )

    # --- dry run: print + trade file, no gRPC, no ledger write ---------------
    def _emit_trade_file(submit: Submit, results, *, is_dry: bool) -> None:
        if not write_trade_file:
            return
        rows = trade_file_mod.build_trade_file_rows(
            trade_date=trade_date,
            submit_id=submit.submit_id,
            payloads=list(submit.payload),
            results=results,
            dry_run=is_dry,
        )
        print(trade_file_mod.format_trade_table(rows))
        dest = trade_file_out or trade_file_mod.default_trade_file_dest(
            trade_date=trade_date,
            submit_id=submit.submit_id,
            strategy_id=strategy_id,
            data_dir=getattr(store, "data_dir", None),
            dry_run=is_dry,
        )
        written = trade_file_mod.write_trade_file(rows, dest)
        print(f"trade file: {written}")

    if dry_run:
        dry = Submit(
            submit_id=pending.submit_id,
            submitted_at=pending.submitted_at,
            env=env,
            ok=True,
            flex_order_ids=(),
            payload=tuple(payloads),
            flex_response={"dry_run": True},
            trade_date=trade_date,
        )
        _emit_trade_file(dry, None, is_dry=True)
        return dry

    submit = submit_flex_orders(
        store,
        payloads,
        env=env,
        trade_date=trade_date,
        adapter=adapter,
        submitted_at=pending.submitted_at,
        submit_id=pending.submit_id,
        claim_submit_id=claim_submit_id,
    )
    results = (submit.flex_response or {}).get("results")
    _emit_trade_file(submit, results, is_dry=False)
    return submit


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
        flex_batch_id=str(result.get("batchId") or "") or None,
        broker=payload.get("broker") or None,
        algo=payload.get("algo") or None,
        order_type=payload.get("orderType") or None,
    )
