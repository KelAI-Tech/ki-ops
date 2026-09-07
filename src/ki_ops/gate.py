"""Pipeline pre-trade gate for the kelaidata ``lseg_strategy_pipeline_combo`` DAG.

Runs right before ``dollar_to_shares`` and validates the **neutralized dollar
book** (and, when available, the converted shares file):

- ``s3://kelaitrading/portfolio/dollar/<strategy_id>/Portfolio_<YYYYMMDD>.csv``
  — header ``SecurityID,$_value``, infocode-keyed signed dollars.
- ``s3://kelaitrading/portfolio/shares/<strategy_id>/Portfolio_<YYYYMMDD>.csv``
  (kelaidata mirrors the dollar book_id subfolder; legacy flat
  ``portfolio/shares/Portfolio_<date>.csv`` also supported) — priced with the
  ds2 H5 prior close, exactly like the KOTL submit path.

``--env dev`` swaps the ``portfolio`` S3 root for ``portfolio_dev``.

Exit contract (Airflow-facing):

- ``0`` — passed (possibly ``"with warnings"``)
- ``1`` — infra error (missing input, S3 failure, bad file…); stdout still
  carries a JSON payload with ``"error_type": "infra"``
- ``2`` — a blocking risk check failed (existing repo convention)

The shares-side check exists because a real pilot run produced a shares book
at +43% net / 74% turnover while its dollar book was +0.17% net — conversion
bugs must be caught before submit, not after.
"""

from __future__ import annotations

import csv
import json
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from ki_ops.checks import CheckViolation, block, warn
from ki_ops.config import load_risk_settings
from ki_ops.engine import format_decimal, passed_status
from ki_ops.kotl.kelaidata_source import (
    DEFAULT_CACHE_DIR,
    DEFAULT_DS2_H5,
    fetch,
    load_ds2_snapshot,
    load_shares_trade_file,
    parse_s3_url,
)
from ki_ops.portfolio import TURNOVER_CONVENTION

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_GATE_CONFIG = ROOT / "config" / "risk_management_poc.yaml"

_S3_ROOTS = {"prod": "portfolio", "dev": "portfolio_dev"}
DOLLAR_TEMPLATE = "s3://kelaitrading/{root}/dollar/{strategy_id}/Portfolio_{yyyymmdd}.csv"
SHARES_STRATEGY_TEMPLATE = "s3://kelaitrading/{root}/shares/{strategy_id}/Portfolio_{yyyymmdd}.csv"
SHARES_FLAT_TEMPLATE = "s3://kelaitrading/{root}/shares/Portfolio_{yyyymmdd}.csv"

_PORTFOLIO_RE = re.compile(r"Portfolio_(\d{8})\.csv$")

_RATIO = Decimal("0.0001")


def _root(env: str) -> str:
    try:
        return _S3_ROOTS[env]
    except KeyError:
        raise ValueError(f"unknown env {env!r} (expected prod or dev)") from None


def dollar_book_path(strategy_id: str, trade_date: date, *, env: str = "prod") -> str:
    return DOLLAR_TEMPLATE.format(
        root=_root(env), strategy_id=strategy_id, yyyymmdd=trade_date.strftime("%Y%m%d")
    )


def shares_candidate_paths(strategy_id: str, trade_date: date, *, env: str = "prod") -> list[str]:
    """Convention shares paths, most specific first (book_id subfolder, then legacy flat)."""
    fmt = dict(root=_root(env), strategy_id=strategy_id, yyyymmdd=trade_date.strftime("%Y%m%d"))
    return [
        SHARES_STRATEGY_TEMPLATE.format(**fmt),
        SHARES_FLAT_TEMPLATE.format(**fmt),
    ]


def load_dollar_book(path: str | Path) -> dict[str, Decimal]:
    """Neutralized dollar book CSV → ordered ``{infocode: signed dollars}``.

    Header ``SecurityID,$_value``; a first row whose second field is not
    numeric is tolerated as the header. Duplicate ids are errors, zero rows
    are dropped (they carry no exposure).
    """
    path = Path(path)
    out: dict[str, Decimal] = {}
    seen: set[str] = set()
    dupes: set[str] = set()
    with path.open(encoding="utf-8", newline="") as fh:
        for i, row in enumerate(csv.reader(fh)):
            if not row or not any(cell.strip() for cell in row):
                continue
            if len(row) < 2:
                raise ValueError(f"{path}:{i + 1}: need at least SecurityID,$_value columns")
            sid = row[0].strip()
            raw = row[1].strip().replace(",", "")
            try:
                dollars = Decimal(raw)
            except InvalidOperation:
                if i == 0:
                    continue  # header row
                raise ValueError(f"{path}:{i + 1}: non-numeric dollar value {row[1]!r}") from None
            if not sid:
                raise ValueError(f"{path}:{i + 1}: empty SecurityID")
            if sid in seen:
                dupes.add(sid)
            seen.add(sid)
            if dollars != 0:
                out[sid] = dollars
    if dupes:
        shown = ", ".join(sorted(dupes)[:20])
        raise ValueError(f"Duplicate SecurityIDs in dollar book {path}: {shown}")
    return out


def _portfolio_file_date(name: str) -> date | None:
    m = _PORTFOLIO_RE.search(name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%d").date()
    except ValueError:
        return None


def find_prior_file(current: str | Path, trade_date: date) -> str | None:
    """Latest ``Portfolio_*.csv`` in *current*'s folder dated strictly before *trade_date*.

    S3 folders are listed with boto3; local folders are globbed.
    """
    text = str(current)
    best: tuple[date, str] | None = None
    if text.startswith("s3://"):
        import boto3

        bucket, key = parse_s3_url(text)
        prefix = key.rsplit("/", 1)[0] + "/"
        pages = boto3.client("s3").get_paginator("list_objects_v2").paginate(
            Bucket=bucket, Prefix=prefix
        )
        for page in pages:
            for obj in page.get("Contents", []):
                d = _portfolio_file_date(obj["Key"].rsplit("/", 1)[-1])
                if d is not None and d < trade_date and (best is None or d > best[0]):
                    best = (d, f"s3://{bucket}/{obj['Key']}")
    else:
        for p in Path(text).resolve().parent.glob("Portfolio_*.csv"):
            d = _portfolio_file_date(p.name)
            if d is not None and d < trade_date and (best is None or d > best[0]):
                best = (d, str(p))
    return best[1] if best else None


def _probe_s3(url: str) -> bool:
    import boto3
    from botocore.exceptions import ClientError

    bucket, key = parse_s3_url(url)
    try:
        boto3.client("s3").head_object(Bucket=bucket, Key=key)
    except ClientError:
        return False
    return True


def _book_stats(dollars_by_id: dict[str, Decimal]) -> tuple[Decimal, Decimal]:
    """(gmv, nmv) of a signed dollar book."""
    gmv = sum((abs(v) for v in dollars_by_id.values()), Decimal("0"))
    nmv = sum(dollars_by_id.values(), Decimal("0"))
    return gmv, nmv


def _two_way_turnover(
    current: dict[str, Decimal], prior: dict[str, Decimal]
) -> tuple[Decimal, Decimal] | None:
    """((buy$ + sell$) / prior GMV, prior GMV) — kelaisim two-way convention."""
    prior_gmv, _ = _book_stats(prior)
    if prior_gmv <= 0:
        return None
    traded = sum(
        (abs(current.get(k, Decimal("0")) - prior.get(k, Decimal("0"))) for k in set(current) | set(prior)),
        Decimal("0"),
    )
    return traded / prior_gmv, prior_gmv


def check_dollar_book(
    book: dict[str, Decimal],
    prior: dict[str, Decimal] | None,
    settings,
    *,
    prior_note: str = "",
) -> tuple[list[CheckViolation], list[CheckViolation], dict[str, Any]]:
    """Net / concentration / turnover / GMV-sanity checks on the dollar book."""
    violations: list[CheckViolation] = []
    warnings: list[CheckViolation] = []
    gmv, nmv = _book_stats(book)
    metrics: dict[str, Any] = {
        "n_names": len(book),
        "gmv": format_decimal(gmv),
        "nmv": format_decimal(nmv),
        "net_exposure": None,
        "turnover": None,
        "prior_gmv": None,
    }

    if gmv <= 0:
        violations.append(block("ZERO_GMV", f"dollar book GMV {gmv} — empty or all-zero book"))
        return violations, warnings, metrics

    net_exposure = abs(nmv) / gmv
    metrics["net_exposure"] = format_decimal(net_exposure, places=_RATIO)
    if net_exposure > settings.max_net_exposure:
        violations.append(
            block(
                "MAX_NET_EXPOSURE",
                f"|net|/GMV {net_exposure:.4f} > max {settings.max_net_exposure}",
            )
        )

    for sid, dollars in book.items():
        concentration = abs(dollars) / gmv
        if concentration > settings.max_position_concentration:
            violations.append(
                block(
                    "MAX_POSITION_CONCENTRATION",
                    f"|{dollars}|/GMV {concentration:.4f} > max {settings.max_position_concentration}",
                    sid,
                )
            )

    if prior is None:
        warnings.append(
            warn(
                "PRIOR_BOOK_MISSING",
                f"no prior Portfolio_*.csv found{prior_note} — turnover check skipped",
            )
        )
    else:
        turn = _two_way_turnover(book, prior)
        if turn is None:
            warnings.append(
                warn("PRIOR_BOOK_EMPTY", "prior book GMV is 0 — turnover check skipped")
            )
        else:
            ratio, prior_gmv = turn
            metrics["turnover"] = format_decimal(ratio, places=_RATIO)
            metrics["prior_gmv"] = format_decimal(prior_gmv)
            if ratio > settings.max_turnover:
                violations.append(
                    block("MAX_TURNOVER", f"{ratio:.4f} > max {settings.max_turnover}")
                )
    return violations, warnings, metrics


def check_shares_book(
    shares: dict[str, Decimal],
    prior_shares: dict[str, Decimal] | None,
    snapshot,
    dollar_book: dict[str, Decimal],
    settings,
    *,
    prior_note: str = "",
) -> tuple[list[CheckViolation], list[CheckViolation], dict[str, Any]]:
    """Recompute the shares book in dollars (ds2 prior close) and re-run the limits.

    Catches conversion bugs: e.g. a +43% net / 74% turnover shares book built
    from a +0.17% net dollar book.
    """
    violations: list[CheckViolation] = []
    warnings: list[CheckViolation] = []

    def _price(book: dict[str, Decimal]) -> tuple[dict[str, Decimal], list[str]]:
        priced: dict[str, Decimal] = {}
        unpriced: list[str] = []
        for ticker, qty in book.items():
            px = snapshot.price(ticker)
            if px is None:
                unpriced.append(ticker)
            else:
                priced[ticker] = qty * px
        return priced, unpriced

    dollars, unpriced = _price(shares)
    if unpriced:
        shown = ", ".join(sorted(unpriced)[:20])
        warnings.append(
            warn(
                "SHARES_UNPRICED",
                f"{len(unpriced)} shares tickers have no ds2 close as of "
                f"{snapshot.px_as_of.isoformat()} (excluded from checks): {shown}",
            )
        )

    gmv, nmv = _book_stats(dollars)
    metrics: dict[str, Any] = {
        "px_as_of": snapshot.px_as_of.isoformat(),
        "n_names": len(shares),
        "n_unpriced": len(unpriced),
        "gmv": format_decimal(gmv),
        "nmv": format_decimal(nmv),
        "net_exposure": None,
        "churn": None,
        "prior_gmv": None,
        "names_dropped": None,
    }

    if gmv <= 0:
        violations.append(block("SHARES_ZERO_GMV", f"shares book GMV {gmv} — empty or unpriced book"))
        return violations, warnings, metrics

    net_exposure = abs(nmv) / gmv
    metrics["net_exposure"] = format_decimal(net_exposure, places=_RATIO)
    if net_exposure > settings.max_net_exposure:
        violations.append(
            block(
                "SHARES_MAX_NET_EXPOSURE",
                f"shares |net|/GMV {net_exposure:.4f} > max {settings.max_net_exposure} "
                "— conversion likely broken, dollar book was inside the limit",
            )
        )

    if prior_shares is None:
        warnings.append(
            warn(
                "SHARES_PRIOR_MISSING",
                f"no prior shares Portfolio_*.csv found{prior_note} — churn check skipped",
            )
        )
    else:
        prior_dollars, _ = _price(prior_shares)
        turn = _two_way_turnover(dollars, prior_dollars)
        if turn is None:
            warnings.append(
                warn("SHARES_PRIOR_EMPTY", "prior shares book GMV is 0 — churn check skipped")
            )
        else:
            ratio, prior_gmv = turn
            metrics["churn"] = format_decimal(ratio, places=_RATIO)
            metrics["prior_gmv"] = format_decimal(prior_gmv)
            if ratio > settings.max_turnover:
                violations.append(
                    block(
                        "SHARES_MAX_TURNOVER",
                        f"shares day-over-day churn {ratio:.4f} > max {settings.max_turnover}",
                    )
                )

    # Names in the dollar book that never made it into the shares file.
    ticker_by_infocode = {sid: t for t, sid in snapshot.infocode_by_ticker.items()}
    dropped = [
        sid
        for sid in dollar_book
        if shares.get(ticker_by_infocode.get(sid, ""), Decimal("0")) == 0
    ]
    metrics["names_dropped"] = len(dropped)
    if dropped:
        shown = ", ".join(dropped[:20])
        warnings.append(
            warn(
                "SHARES_NAMES_DROPPED",
                f"{len(dropped)} dollar-book names missing or zero in the shares file: {shown}"
                f"{' …' if len(dropped) > 20 else ''}",
            )
        )
    return violations, warnings, metrics


def write_verdict(dest: str, payload: dict[str, Any]) -> str:
    """Write the verdict JSON to a local path or an ``s3://`` URI."""
    body = json.dumps(payload, indent=2, default=str) + "\n"
    if dest.startswith("s3://"):
        import boto3

        bucket, key = parse_s3_url(dest)
        boto3.client("s3").put_object(
            Bucket=bucket, Key=key, Body=body.encode("utf-8"), ContentType="application/json"
        )
        return dest
    out = Path(dest)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(body, encoding="utf-8")
    return str(out)


def run_gate_checks(
    *,
    strategy_id: str,
    trade_date: date,
    env: str = "prod",
    dollar_file: str | None = None,
    prior_file: str | None = None,
    shares_file: str | None = None,
    ds2: str | None = None,
    config: str | Path | None = None,
    cache_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Resolve inputs, run all gate checks, and build the verdict payload.

    Raises on infra problems (missing files, S3 errors, unparseable books) —
    :func:`run_gate` turns those into the exit-1 JSON contract.
    """
    from ki_ops import __version__
    from ki_ops.audit import input_hashes, settings_hash

    config = config or DEFAULT_GATE_CONFIG
    cache = cache_dir or DEFAULT_CACHE_DIR
    settings = load_risk_settings(config)

    dollar_spec = dollar_file or dollar_book_path(strategy_id, trade_date, env=env)
    dollar_local = fetch(dollar_spec, cache_dir=cache)
    book = load_dollar_book(dollar_local)

    prior_spec = prior_file or find_prior_file(dollar_spec, trade_date)
    prior_local = fetch(prior_spec, cache_dir=cache) if prior_spec else None
    prior = load_dollar_book(prior_local) if prior_local else None

    violations, warnings, dollar_metrics = check_dollar_book(
        book, prior, settings, prior_note=f" before {trade_date.isoformat()} next to {dollar_spec}"
    )

    # Shares side: explicit --shares-file always runs; otherwise probe the S3
    # convention paths only when the dollar book itself came from S3 convention
    # (a local --dollar-file run is offline — never touch the network for it).
    shares_spec = shares_file
    if shares_spec is None and dollar_file is None:
        shares_spec = next(
            (u for u in shares_candidate_paths(strategy_id, trade_date, env=env) if _probe_s3(u)),
            None,
        )

    shares_metrics: dict[str, Any] | None = None
    shares_local = None
    ds2_local = None
    prior_shares_spec = None
    prior_shares_local = None
    if shares_spec is not None:
        shares_local = fetch(shares_spec, cache_dir=cache)
        ds2_local = fetch(ds2 or DEFAULT_DS2_H5, cache_dir=cache)
        shares = load_shares_trade_file(shares_local)
        snapshot = load_ds2_snapshot(ds2_local, trade_date=trade_date)

        prior_shares_spec = find_prior_file(shares_spec, trade_date)
        prior_shares_local = fetch(prior_shares_spec, cache_dir=cache) if prior_shares_spec else None
        prior_shares = load_shares_trade_file(prior_shares_local) if prior_shares_local else None

        s_violations, s_warnings, shares_metrics = check_shares_book(
            shares,
            prior_shares,
            snapshot,
            book,
            settings,
            prior_note=f" before {trade_date.isoformat()} next to {shares_spec}",
        )
        violations += s_violations
        warnings += s_warnings
        shares_metrics = {
            "file": str(shares_spec),
            "prior_file": str(prior_shares_spec) if prior_shares_spec else None,
            **shares_metrics,
        }

    hashes = input_hashes(
        {
            "config": config,
            "dollar": dollar_local,
            "prior": prior_local,
            "shares": shares_local,
            "prior_shares": prior_shares_local,
            "ds2": ds2_local,
        }
    )
    allowed = not violations
    return {
        "command": "gate",
        "ki_ops_version": __version__,
        "strategy_id": strategy_id,
        "trade_date": trade_date.isoformat(),
        "env": env,
        "config": str(config),
        "config_hash": settings_hash(settings),
        "input_hashes": hashes,
        "dollar_file": str(dollar_spec),
        "prior_file": str(prior_spec) if prior_spec else None,
        "max_net_exposure": format_decimal(settings.max_net_exposure, places=_RATIO),
        "max_position_concentration": format_decimal(
            settings.max_position_concentration, places=_RATIO
        ),
        "max_turnover": format_decimal(settings.max_turnover, places=_RATIO),
        "turnover_convention": TURNOVER_CONVENTION,
        "dollar": dollar_metrics,
        "shares": shares_metrics,
        "corp_action_check": "not_implemented",
        "passed": passed_status(allowed, warnings),
        "violation_codes": sorted({v.code for v in violations}),
        "violations": [v.to_dict() for v in violations],
        "warning_codes": sorted({v.code for v in warnings}),
        "warnings": [v.to_dict() for v in warnings],
    }


def register_gate_parser(sub) -> None:
    g = sub.add_parser(
        "gate",
        help="kelaidata pipeline pre-trade gate: neutralized dollar book (+ shares) before dollar_to_shares",
    )
    g.add_argument("--strategy-id", required=True, help="book id (dollar/<strategy_id>/ S3 subfolder)")
    g.add_argument("--trade-date", type=date.fromisoformat, required=True, help="YYYY-MM-DD")
    g.add_argument(
        "--env",
        choices=("prod", "dev"),
        default="prod",
        help="prod → s3://kelaitrading/portfolio/…, dev → portfolio_dev",
    )
    g.add_argument(
        "--dollar-file",
        default=None,
        help="dollar book override, s3:// or local (default: convention path for --strategy-id)",
    )
    g.add_argument(
        "--prior-file",
        default=None,
        help="prior dollar book override (default: latest Portfolio_*.csv < trade date in the same folder)",
    )
    g.add_argument(
        "--shares-file",
        default=None,
        help="converted shares file, s3:// or local (default: convention path if it exists; else skipped)",
    )
    g.add_argument(
        "--ds2",
        default=None,
        help=f"ds2 H5 for shares pricing, s3:// or local (default: {DEFAULT_DS2_H5})",
    )
    g.add_argument(
        "--config",
        dest="gate_config",
        default=str(DEFAULT_GATE_CONFIG),
        help="risk YAML (defaults to config/risk_management_poc.yaml)",
    )
    g.add_argument(
        "--json-out",
        default=None,
        help="also write the verdict JSON to a local path or an s3:// URI",
    )
    g.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="S3 download cache (default: data/kotl/cache)",
    )


def run_gate(args) -> int:
    """Gate entry point with the Airflow exit contract (0 pass / 1 infra / 2 block)."""
    from ki_ops import __version__

    try:
        payload = run_gate_checks(
            strategy_id=args.strategy_id,
            trade_date=args.trade_date,
            env=args.env,
            dollar_file=args.dollar_file,
            prior_file=args.prior_file,
            shares_file=args.shares_file,
            ds2=args.ds2,
            config=args.gate_config,
            cache_dir=args.cache_dir,
        )
        if args.json_out:
            write_verdict(args.json_out, payload)
    except Exception as exc:  # noqa: BLE001 — the DAG needs JSON + exit 1, never a bare traceback
        print(
            json.dumps(
                {
                    "passed": False,
                    "error": str(exc),
                    "error_type": "infra",
                    "ki_ops_version": __version__,
                },
                indent=2,
                default=str,
            )
        )
        return 1
    print(json.dumps(payload, indent=2, default=str))
    return 0 if payload["passed"] else 2
